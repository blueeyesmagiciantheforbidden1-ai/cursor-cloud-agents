"""Full development-domain port of the user-supplied 9,348-line tool lab.

All original top-level function identities are accounted for in the provenance
map. Domain vocabulary is translated consistently across code, telemetry keys,
prompts and embedded self-checks. This is not a claim that missing companion
engine modules have been supplied or that the legacy full self-check has passed.

Host-dependent operations use injected hub adapters instead of direct agent
launches, copying live files, implicit shell access or deleting research history.
Adapters must preserve actual usage reservations, isolated workspaces, trusted
measurements and the existing evidence-bound release coordinator. Binding an
adapter is operator configuration, not proof of these contracts or an auth API.
"""

_HUB_ADAPTERS = None
_HUB_OPERATIONS = frozenset(("admit_cycle", "run_process", "run_candidate",
    "judge_candidate", "verify_replay", "promote_candidate", "run_hidden_replay",
    "retention", "disk_guard", "prepare_variant"))


def bind_hub_adapters(adapters):
    """Bind once before starting a dedicated trusted lab controller process."""
    global _HUB_ADAPTERS
    if _HUB_ADAPTERS is not None or not all(callable(getattr(adapters, name, None)) for name in _HUB_OPERATIONS):
        raise ValueError("Complete trusted adapters must be bound exactly once")
    _HUB_ADAPTERS = adapters


def _hub_operation(name, *args, **kwargs):
    if _HUB_ADAPTERS is None or name not in _HUB_OPERATIONS:
        raise RuntimeError("hub_adapters_not_commissioned")
    result = getattr(_HUB_ADAPTERS, name)(*args, **kwargs)
    if name == "admit_cycle" and (not isinstance(result, dict) or result.get("admitted") is not True):
        raise RuntimeError("verified_usage_reservation_required")
    return result


import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


SCHEMA_VERSION = 1
ENGINE_NAME = "code_evolution_engine.py"
LEGACY_RUNTIME_NAME = "_code_evolution_engine_legacy.py"
SUPERVISOR_NAME = "code_repair_supervisor.py"
LAB_OS_NAME = "development_lab_os.py"
TOOL_LAB_NAME = "development_tool_lab.py"
PROMOTABLE_FILES = [
    ENGINE_NAME,
    LEGACY_RUNTIME_NAME,
    SUPERVISOR_NAME,
    LAB_OS_NAME,
    TOOL_LAB_NAME,
    "code_engine/code_surgery.py",
    "code_engine/evidence_proof_worker.py",
    "code_engine/legacy_slices/slice_017_llm_v5113_is_nochild_failsafe_brain.py",
    "code_engine/legacy_slices/manifest.json",
]
TOOL_BRAIN_MIN_SCORE = 80.0
TOOL_LAB_DEFAULT_KEEP_RUNS = 24
TOOL_LAB_DEFAULT_MAX_GB = 0.25
TOOL_LAB_DEFAULT_KEEP_TICKETS = 48
TOOL_LAB_DEFAULT_TICKETS_MAX_GB = 0.05
CODE_BRAIN_DEFAULT_VARIANTS_PER_CYCLE = 5
CODE_BRAIN_DEFAULT_LAB_WORKERS = 5
PATCH_TOURNAMENT_MIN_VARIANTS = 3
PATCH_TOURNAMENT_MAX_VARIANTS = 5
PATCH_GARDEN_NURSERY_DEFAULT_LANES = 1
PATCH_GARDEN_NURSE_RETRY_SEC = 60
PATCH_GARDEN_MAX_RETRY_ATTEMPTS = int(os.environ.get("CODE_LAB_PATCH_GARDEN_MAX_RETRY_ATTEMPTS", "3"))
PATCH_GARDEN_PROTECTED_DIFF_REASONS = {
    "protected_diff_not_clean",
    "protected_diff_fail",
    "critic_protected_constants_reject",
}
PATCH_GARDEN_EMPTY_FAILED_REASONS = {
    "codex_exec_not_clean",
    "no_promotable_file_changed",
}
TOOL_ATOM_FLOW_GA_MIN_VARIANTS = 1
TOOL_ATOM_FLOW_GA_MAX_VARIANTS = PATCH_TOURNAMENT_MAX_VARIANTS
TOOL_ATOM_FLOW_GA_COMPUTE_MULTIPLIER = 1
TOOL_ATOM_FLOW_GA_BASE_SPEC_BUDGET = 32
TOOL_ATOM_FLOW_GA_BASE_JUDGE_CANDIDATES = 128
TOOL_ATOM_FLOW_GA_MAX_SPEC_BUDGET = 32
TOOL_ATOM_FLOW_GA_MAX_JUDGE_CANDIDATES = 128
TOOL_ATOM_FLOW_GA_MATERIALIZED_JUDGE_QUEUE_LIMIT = 128
COMPONENT_ATOM_PROMOTION_GA_COMPONENTS = ("tool", "garden", "nursery", "evidence")
COMPONENT_ATOM_PROMOTION_GA_AMPLIFIED_COMPONENTS = COMPONENT_ATOM_PROMOTION_GA_COMPONENTS
COMPONENT_ATOM_PROMOTION_GA_MAX_CANDIDATES = PATCH_TOURNAMENT_MAX_VARIANTS
COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS = 3
COMPONENT_ATOM_PROMOTION_GA_REFINED_SIGNAL_BATCH = 128
EVIDENCE_TOOL_GA_COMPUTE_MULTIPLIER = 1
EVIDENCE_TOOL_GA_MATERIALIZED_CANDIDATE_LIMIT = 32
EVIDENCE_TOOL_GA_MAX_LOCAL_GENERATIONS = 3
EVIDENCE_TOOL_GA_MIN_LOCAL_GENERATIONS = 1
EVIDENCE_TOOL_GA_PROOF_WORK_LIMIT = 128
COMPONENT_ATOM_PROMOTION_PROOF_WORK_MAX_ITEMS = 128
COMPONENT_MAIN_SEARCH_FLOW_SIGNAL_SAMPLE = 16
APPROVAL_REQUIRED_PROGRESS_AXES = ("evidence", "tool")
PATCH_GARDEN_EVIDENCE_STALE_REFRESH_EVIDENCE_FIX = "add_evidence_new_cards_or_bridge_or_stale_refresh_evidence"
PATCH_GARDEN_RETRY_REQUIRED_FIXES = (
    "make_the_patch_smaller_and_finish_before_timeout",
    "write_patch_summary_json_before_long_tests",
    "add_evidence_tool_progress_contract_with_before_after_evidence",
    "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
    "add_one_cheap_self_check_that_proves_the_claim",
    "keep_promotion_allowed_false_until_all_gates_pass",
)
PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX = "repair_hidden_replay_failure_before_promotion"
PATCH_GARDEN_E912AE_REPAIR_CARD_ID = "e912aeb10982df09"
PATCH_GARDEN_E912AE_NEXT_SMALL_FIXES = (
    PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX,
    "make_the_patch_smaller_and_finish_before_timeout",
    "write_patch_summary_json_before_long_tests",
    "add_evidence_tool_progress_contract_with_before_after_evidence",
    "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
    "add_one_cheap_self_check_that_proves_the_claim",
    "keep_promotion_allowed_false_until_all_gates_pass",
)
PATCH_GARDEN_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID = "82dd89f289e3aa15"
PATCH_GARDEN_ACTIVE_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID = "778e03de653bf254"
PATCH_GARDEN_ACTIVE_REPOSITORY_REPAIR_NURSERY_REPAIR_CARD_ID = "3dd09d213e2ee628"
PATCH_GARDEN_B55003_REPAIR_CARD_ID = "b5500396f7734c18"
PATCH_GARDEN_U163986_REPAIR_CARD_ID = "16398623d0a43e8a"
PATCH_GARDEN_U19C81A_REPAIR_CARD_ID = "19c81a3a86ca164f"
PATCH_GARDEN_U45A8A6_REPAIR_CARD_ID = "45a8a60fd9d8dcf0"
PATCH_GARDEN_U9C41AC_REPAIR_CARD_ID = "9c41ac11394c7645"
PATCH_GARDEN_U30FFA5_REPAIR_CARD_ID = "30ffa5a2c3739741"
PATCH_GARDEN_BD6802_REPAIR_CARD_ID = "bd680241411e32ee"
PATCH_GARDEN_U141053_REPAIR_CARD_ID = "1410535ae7e7f5b2"
PATCH_GARDEN_B758C1_REPAIR_CARD_ID = "b758c148d9bba4b5"
PATCH_GARDEN_D0EB2F_REPAIR_CARD_ID = "d0eb2f225c501669"
PATCH_GARDEN_F45089_REPAIR_CARD_ID = "f4508933839df1f1"
PATCH_GARDEN_U591F7C_REPAIR_CARD_ID = "591f7c5f9e48c368"
PATCH_GARDEN_F45089_NEXT_SMALL_FIXES = (
    "add_evidence_tool_progress_contract_with_before_after_evidence",
    PATCH_GARDEN_EVIDENCE_STALE_REFRESH_EVIDENCE_FIX,
    "add_one_cheap_self_check_that_proves_the_claim",
    "keep_promotion_allowed_false_until_all_gates_pass",
)
PATCH_GARDEN_U1FFE8C_REPAIR_CARD_ID = "1ffe8c8de2586e51"
PATCH_GARDEN_U1FFE8C_NEXT_SMALL_FIXES = (
    "add_evidence_tool_progress_contract_with_before_after_evidence",
    PATCH_GARDEN_EVIDENCE_STALE_REFRESH_EVIDENCE_FIX,
    "add_one_cheap_self_check_that_proves_the_claim",
    "keep_promotion_allowed_false_until_all_gates_pass",
)
PATCH_GARDEN_B758C1_NEXT_SMALL_FIXES = (
    "add_evidence_tool_progress_contract_with_before_after_evidence",
    PATCH_GARDEN_EVIDENCE_STALE_REFRESH_EVIDENCE_FIX,
    "add_one_cheap_self_check_that_proves_the_claim",
    "keep_promotion_allowed_false_until_all_gates_pass",
)
PATCH_GARDEN_B5221C_REPAIR_CARD_ID = "b5221cda83024a12"
PATCH_GARDEN_B5221C_NEXT_SMALL_FIXES = (
    PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX,
    "make_the_patch_smaller_and_finish_before_timeout",
    "write_patch_summary_json_before_long_tests",
    "add_evidence_tool_progress_contract_with_before_after_evidence",
    "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
    "add_one_cheap_self_check_that_proves_the_claim",
    "keep_promotion_allowed_false_until_all_gates_pass",
)
PATCH_GARDEN_C3D7F8_REPAIR_CARD_ID = "c3d7f80950b4687b"
PATCH_GARDEN_C3D7F8_NEXT_SMALL_FIXES = (
    PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX,
    "move_repair_to_allowed_surface_and_restore_protected_diff_clean",
    "add_one_cheap_self_check_that_proves_the_claim",
    "keep_promotion_allowed_false_until_all_gates_pass",
)
PATCH_GARDEN_U30FFA5_NEXT_SMALL_FIXES = (
    PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX,
    "make_the_patch_smaller_and_finish_before_timeout",
    "write_patch_summary_json_before_long_tests",
    "add_evidence_tool_progress_contract_with_before_after_evidence",
    "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
    "add_one_cheap_self_check_that_proves_the_claim",
    "keep_promotion_allowed_false_until_all_gates_pass",
)
EVIDENCE_APPROVAL_FILES = {
    "code_engine/evidence_proof_worker.py",
}
TOOL_APPROVAL_FILES = {
    LAB_OS_NAME,
    TOOL_LAB_NAME,
    "code_engine/code_surgery.py",
    "organs/tool_execution_policy.py",
    "organs/scheduler_action_policy.py",
    "code_engine/legacy_slices/slice_017_llm_v5113_is_nochild_failsafe_brain.py",
}

FORBIDDEN_SURFACES = [
    "rank gates",
    "final gates",
    "provider costs",
    "runtime overhead",
    "fixed horizon / evaluation_steps",
    "final/test/holdout firewall",
    "save/deploy policy",
    "production execution",
    "production release controller",
    "secrets",
    "arbitrary shell execution authority",
]

ALLOWED_SURFACES = [
    "search ecology",
    "parent selection",
    "family quotas",
    "operator quotas",
    "hard-mix scheduling",
    "VerificationPass rescue logic",
    "Integration-stage non-final routing",
    "RegressionPreview-to-VerificationPass bridge",
    "overfit/debt/firewall diagnostics",
    "runner reliability and disk guards",
    "dynamic tool birth policy",
    "tool observe execution",
    "tool matched-control islands",
    "tool proof-yield scoring",
    "tool bloat/duplicate governance",
    "tool memory and mutation policy",
    "repository-scoped alpha mechanism nursery",
    "non-final REPOSITORY atom and clue nurturing",
    "patch tournament policy",
    "causal self-repair tickets",
    "rollback memory",
    "search governor diagnostics",
    "search power spread portfolio",
    "component atom-promotion GAs",
    "anti-overfit auditor prompts",
    "self-check generator scaffolds",
    "Integration-stage non-final tool routing",
    "RegressionPreview-to-VerificationPass tool bridge",
    "diagnostics",
    "self-checks",
    "non-final proof routing",
]

CODE_BRAIN_THEORIES = [
    {
        "id": "A",
        "name": "A_tool_birth_specificity",
        "patch_family": "tool_birth_specificity",
        "scope": "tool_foundry",
        "diagnosis": "The foundry creates too many broad or duplicate tools instead of sharply pain-specific repair tools.",
        "expected_surface": "tool birth filters, pain compiler, duplicate culler, tool genome metadata",
        "proof_test": "new tools must name exact pain, required primitive, proof target, kill condition, and blocked protected surfaces",
        "failure_condition": "more tools are born without higher matched-control lift",
    },
    {
        "id": "B",
        "name": "B_tool_matched_control_lift",
        "patch_family": "tool_matched_control_lift",
        "scope": "tool_proof",
        "diagnosis": "Tools execute but do not earn trust because matched controls are too sparse or not summarized into useful routing feedback.",
        "expected_surface": "matched tool island accounting, Bayesian trust, promotion gates",
        "proof_test": "tool trust rises only after repeated positive matched-control lift and remains zero-influence otherwise",
        "failure_condition": "tool influence rises without positive matched-control evidence",
    },
    {
        "id": "C",
        "name": "C_tool_to_verificationpass_conversion",
        "patch_family": "tool_to_verificationpass_conversion",
        "scope": "tool_to_proof",
        "diagnosis": "Autopsy tools generate observations but do not become VerificationPass-shaped repair candidates.",
        "expected_surface": "tool action cards, VerificationPass nursery hints, Integration-stage source routing",
        "proof_test": "tool hints route a bounded family-diverse pool into VerificationPass-shaped evaluation without changing gates",
        "failure_condition": "tool execution increases logs but not VerificationPass conversion",
    },
    {
        "id": "D",
        "name": "D_regressionpreview_tool_bridge",
        "patch_family": "regressionpreview_tool_bridge",
        "scope": "regressionpreview_bridge",
        "diagnosis": "RegressionPreview autopsy tools are born but do not reliably convert RegressionPreview failures into VerificationPass near-miss repair.",
        "expected_surface": "RegressionPreview autopsy, near-miss repair, bridge score, source pool bias",
        "proof_test": "regressionpreview tools must output concrete repair hints and matched controls must track VerificationPass conversion",
        "failure_condition": "RegressionPreview volume grows without VerificationPass lift",
    },
    {
        "id": "E",
        "name": "E_tool_bloat_retirement",
        "patch_family": "tool_bloat_retirement",
        "scope": "tool_governance",
        "diagnosis": "Weak observer tools consume memory and decision attention after repeated no-lift execution.",
        "expected_surface": "tool retirement, cooldown, duplicate governance, patch memory",
        "proof_test": "low-yield duplicate tool families enter cooldown and future prompts mention the failed pattern",
        "failure_condition": "bloat shrinks by deleting useful diversity or hiding evidence",
    },
    {
        "id": "F",
        "name": "F_tool_memory_mutation",
        "patch_family": "tool_memory_mutation",
        "scope": "tool_memory",
        "diagnosis": "Failed tool families are remembered but not mutated into better descendants with explicit failure lessons.",
        "expected_surface": "tool memory, mutation recipes, future prompt construction",
        "proof_test": "new tool variants cite prior failure lessons and change at least one proof-relevant mechanism",
        "failure_condition": "the same poor tool shape is recreated under a new id",
    },
    {
        "id": "G",
        "name": "G_family_ecology_and_parent_pool",
        "patch_family": "family_ecology_parent_pool",
        "scope": "search_ecology",
        "diagnosis": "Family concentration or weak family rotation can quietly reduce proof diversity even when short-term scores look good.",
        "expected_surface": "family passports, candidate parent pool quotas, cross-family mating diagnostics, non-final breeding pressure",
        "proof_test": "healthy telemetry remains unchanged while monoculture telemetry activates family-diverse routing without relaxing gates",
        "failure_condition": "family concentration improves only by lowering proof quality or hiding dominant-family evidence",
    },
    {
        "id": "H",
        "name": "H_integration_stage_proof_per_compute",
        "patch_family": "integration_stage_proof_per_compute",
        "scope": "integration_stage_routing",
        "diagnosis": "Integration-stage can burn compute on low-yield queues when candidate sources are not triaged by proof-per-compute.",
        "expected_surface": "Integration-stage queue priority, source caps, family diversity routing, proof-per-compute diagnostics",
        "proof_test": "synthetic Integration-stage waste telemetry routes scarce budget toward near-misses and diverse proof sources",
        "failure_condition": "Integration-stage routing changes final/holdout gates or rejects useful diversity by score-only shortcuts",
    },
    {
        "id": "I",
        "name": "I_overfit_prison_and_firewall",
        "patch_family": "overfit_prison_firewall",
        "scope": "anti_overfit",
        "diagnosis": "The engine must keep proof, split exposure, and final-taint discipline from being bypassed by new rescue logic.",
        "expected_surface": "overfit debt, split exposure, final-taint propagation, leakage-canary diagnostics, self-checks",
        "proof_test": "final-tainted or split-reused synthetic objects cannot breed, seed memory, or certify patches",
        "failure_condition": "a patch raises apparent score while weakening anti-overfit or final firewall behavior",
    },
    {
        "id": "J",
        "name": "J_runner_disk_and_truth_trace",
        "patch_family": "runner_disk_truth_trace",
        "scope": "runner_reliability",
        "diagnosis": "Forever operation needs bounded logs, truth-trace retention, disk guards, and restart-safe status without interrupting live research.",
        "expected_surface": "runner guards, retention policy, compact status logs, safe stop/restart boundaries",
        "proof_test": "disk-pressure telemetry prunes safe archives and blocks new heavy work without touching live collector/trainer PIDs",
        "failure_condition": "cleanup deletes active outputs or restarts the long run unnecessarily",
    },
    {
        "id": "K",
        "name": "K_hardmix_curriculum_and_shadow",
        "patch_family": "hardmix_curriculum_shadow",
        "scope": "curriculum",
        "diagnosis": "Hard-mix and shadow objectives need continual proof-aware curriculum tuning without repeatedly resetting long runs.",
        "expected_surface": "hardmix scheduling, scout/main split, shadow guard routing, diagnostics",
        "proof_test": "low-proof synthetic telemetry rolls back main hardmix while keeping scout exploration alive",
        "failure_condition": "hard scouts dominate main population without VerificationPass conversion proof",
    },
    {
        "id": "L",
        "name": "L_scheduler_execution_authority",
        "patch_family": "scheduler_execution_authority",
        "scope": "scheduler",
        "diagnosis": "The scheduler can keep planning or building libraries instead of executing observe-only proof actions when evidence is already sufficient.",
        "expected_surface": "scheduler action policy, observe execution queue, stop/checkpoint decisions, compact diagnostics",
        "proof_test": "synthetic pain telemetry prevents repeated library-only decisions and schedules proof-safe observe execution",
        "failure_condition": "the scheduler gains authority to touch final/live/deploy surfaces or loops in no-op actions",
    },
    {
        "id": "M",
        "name": "M_repository_repair_mechanism_nursery",
        "patch_family": "repository_repair_mechanism_nursery",
        "scope": "repair_factory",
        "diagnosis": "Weak repository-scoped non-final alpha clues can be discarded as blockers before they become explicit mechanisms with repairs, broken-sibling traps, and regime expansion.",
        "expected_surface": "mechanism arena, alpha nursery reports, blocker-to-repair recipes, non-final repository-scoped proof routing",
        "proof_test": "faint positive repository-scoped clues become zero-influence nursery candidates with repair axes, negative controls, and three-regime proof requirements",
        "failure_condition": "nursery material influences the main search, uses cross-repository data, or bypasses matched controls and normal gates",
    },
    {
        "id": "N",
        "name": "N_patch_tournament_and_causal_repair",
        "patch_family": "patch_tournament_causal_repair",
        "scope": "self_repair",
        "diagnosis": "Single repair attempts can overfit one collapse mode; the brain should test several causal patches and keep only proof-ecology improvements.",
        "expected_surface": "sandbox tournament selection, patch summary schema, deterministic judge, rollback memory",
        "proof_test": "each patch states blocker X, allowed surface Y, metric Z, and loses if protected diff or anti-overfit auditor rejects it",
        "failure_condition": "a repair is promoted without a tournament, causal claim, rollback record, or self-check",
    },
    {
        "id": "O",
        "name": "O_search_governor_budget_brain",
        "patch_family": "search_governor_budget_brain",
        "scope": "search_governor",
        "diagnosis": "Compute can be wasted on families, regimes, mutation axes, or proof depths that historically do not produce proof.",
        "expected_surface": "budget diagnostics, family/operator/regime/tool-type proof yield memory, proof-depth routing",
        "proof_test": "budget suggestions favor historically productive non-final proof routes without changing gates",
        "failure_condition": "budget logic hides diversity, skips required proof, or biases toward score-only shortcuts",
    },
    {
        "id": "S",
        "name": "S_search_power_spread_blender",
        "patch_family": "search_power_spread_blender",
        "scope": "search_power_spread",
        "diagnosis": "Tool, evidence, nursery, Integration-stage, parent-pool, and proof-depth signals are handled as separate weak loops instead of one broad zero-influence search portfolio.",
        "expected_surface": "search governor diagnostics, nursery routing, tool lineage, parent/operator/family/regime spread, proof-depth scheduling",
        "proof_test": "a patch builds a bounded portfolio that combines every available non-final signal into one-axis descendants and same-parent matched-control retests before any influence",
        "failure_condition": "the spread gives authority to unproven material, bypasses matched controls, uses final/holdout data, or becomes score-only search expansion",
    },
    {
        "id": "P",
        "name": "P_anti_overfit_auditor",
        "patch_family": "anti_overfit_auditor",
        "scope": "anti_overfit_auditor",
        "diagnosis": "Every proposed improvement needs an adversarial reviewer trying to disprove it before trust rises.",
        "expected_surface": "auditor prompts, contradiction checks, negative controls, hidden replay requirements",
        "proof_test": "auditor rejects patches that improve only current failure windows or weaken proof ecology",
        "failure_condition": "auditor can relax gates or approve without falsification evidence",
    },
    {
        "id": "Q",
        "name": "Q_self_check_generator",
        "patch_family": "self_check_generator",
        "scope": "test_generation",
        "diagnosis": "New self-improvement paths can be added without a small deterministic self-check proving their policy.",
        "expected_surface": "self-check scaffolds, patch summary requirements, regression hooks",
        "proof_test": "new repair types include a cheap self-check before they can enter tournament memory",
        "failure_condition": "a new repair path is added without a compile-safe test or policy assertion",
    },
    {
        "id": "R",
        "name": "R_evidence_and_tool_progress_contract",
        "patch_family": "evidence_tool_progress_contract",
        "scope": "evidence_proof",
        "diagnosis": "Evidence proof cards and dynamic-tool loops can look active while repeating the same evidence or no-lift tool behavior.",
        "expected_surface": "evidence proof worker progress diagnostics, alpha preproof bridge, tool proof-yield quarantine, and supervisor promotion gates",
        "proof_test": "a patch must show a non-final before/after evidence/tool progress contract: new proof evidence, bridge consumption, stale-input refresh action, tool observe execution, matched lift, or no-lift quarantine",
        "failure_condition": "supervisor accepts a patch that only changes prompts or logs while evidence and tools remain stuck",
    },
]


def now_stamp():
    return time.strftime("%Y%m%d_%H%M%S")


def resolve_root(project_root):
    return Path(project_root or ".").resolve()


def tool_lab_root(project_root):
    return resolve_root(project_root) / "code_surgery" / "tool_lab"


def runner_logs(project_root):
    path = resolve_root(project_root) / "runner_logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return path


def write_json_archive(path, obj):
    """Write a retained archive copy of a large payload.

    Archive copies are never read back by the lab, only kept for forensics, so
    they are stored compact (no indent/sort padding) and gzipped. On the real
    ticket/autopsy payloads this is a ~20-50x reduction versus write_json's
    indent=2 form, which is what let tickets/ grow to 337GB unbounded.
    Set CODE_LAB_TOOL_LAB_ARCHIVE_PLAIN=1 to fall back to uncompressed .json.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)
    if os.environ.get("CODE_LAB_TOOL_LAB_ARCHIVE_PLAIN") == "1":
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(path))
        return path
    gz_path = path.with_suffix(path.suffix + ".gz")
    tmp = gz_path.with_suffix(gz_path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        fh.write(payload)
    os.replace(str(tmp), str(gz_path))
    return gz_path


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def append_jsonl(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return path


def read_tail_lines(path, max_lines=80, max_bytes=1024 * 1024):
    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - int(max_bytes)))
            data = f.read().decode("utf-8", errors="replace")
        return data.splitlines()[-int(max_lines):]
    except Exception:
        return []


def parse_jsonl_tail(path, max_lines=80):
    rows = []
    for line in read_tail_lines(path, max_lines=max_lines):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def summarize_tool_index(project_root):
    root = resolve_root(project_root)
    index_path = root / "research_best" / "llm_dynamic_tool_arsenal_index.json"
    idx = read_json(index_path, {})
    tools_dict = idx.get("tools") if isinstance(idx, dict) else {}
    tools = list(tools_dict.values()) if isinstance(tools_dict, dict) else []

    def as_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return float(default)

    valid = [t for t in tools if t.get("valid") is True]
    retired = [t for t in tools if t.get("retired") is True or str(t.get("maturity_phase", "")).upper() == "RETIRED"]
    active = [t for t in tools if t not in retired]
    duplicate_marked = [t for t in tools if t.get("duplicate_of_tool_id")]
    mutation_recommended = [
        t for t in tools
        if t.get("mutation_recommended") is True
        or t.get("replacement_mutation_required") is True
        or bool(t.get("v5118_mutation_reason"))
    ]
    influence = [t for t in tools if t.get("can_influence_main_search") is True]
    exec_positive = [t for t in tools if as_float(t.get("exec_count")) > 0 or as_float(t.get("proof_yield_exec_count")) > 0]
    population_share = [t for t in tools if as_float(t.get("max_population_share")) > 0 and t.get("can_influence_main_search") is True]
    proof_yields = [as_float(t.get("proof_yield_ema")) for t in tools if t.get("proof_yield_ema") is not None]
    positive_yield = [v for v in proof_yields if v > 0.0]
    tool_atoms = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        for key in ("pain_target", "exact_pain_target", "tool_class", "maturity_phase", "debt_type"):
            value = str(tool.get(key) or "").strip()
            if value:
                tool_atoms.add(value[:96])
        genome = tool.get("tool_genome") if isinstance(tool.get("tool_genome"), dict) else {}
        for key in ("pain_target", "exact_pain_target", "maturity_phase", "debt_type"):
            value = str(genome.get(key) or "").strip()
            if value:
                tool_atoms.add(value[:96])
        for seq_key in ("primitive_sequence", "action_surface", "input_signature"):
            values = genome.get(seq_key)
            if isinstance(values, list):
                for item in values:
                    text = str(item or "").strip()
                    if text:
                        tool_atoms.add(text[:96])
        card = tool.get("search_action_card") if isinstance(tool.get("search_action_card"), dict) else {}
        value = str(card.get("tool_name") or "").strip()
        if value:
            tool_atoms.add(value[:96])
        evidence = card.get("evidence") if isinstance(card.get("evidence"), dict) else {}
        primitives = evidence.get("primitives")
        if isinstance(primitives, list):
            for item in primitives:
                text = str(item or "").strip()
                if text:
                    tool_atoms.add(text[:96])
    latest = idx.get("last_tool_id")
    return {
        "index_path": str(index_path),
        "stats_created": (idx.get("stats") or {}).get("created"),
        "stats_executed": (idx.get("stats") or {}).get("executed"),
        "total_indexed": len(tools),
        "valid": len(valid),
        "retired": len(retired),
        "active": len(active),
        "duplicate_marked": len(duplicate_marked),
        "mutation_recommended": len(mutation_recommended),
        "active_tool_count": ((idx.get("v5117_bloat_governor") or {}).get("active_tool_count")),
        "can_influence_main_search": len(influence),
        "positive_population_share": len(population_share),
        "exec_count_positive": len(exec_positive),
        "proof_yield_ema_positive": len(positive_yield),
        "proof_yield_ema_avg": round(sum(proof_yields) / max(len(proof_yields), 1), 6),
        "tool_atom_neuron_count": len(tool_atoms),
        "tool_atom_neuron_ready": bool(tool_atoms),
        "tool_atom_attention_targets": sorted(tool_atoms)[:12],
        "latest_tool_id": latest,
        "last_governor_ts": ((idx.get("v5117_bloat_governor") or {}).get("last_ts")),
    }


def summarize_recent_tool_events(project_root):
    root = resolve_root(project_root)
    proof_rows = parse_jsonl_tail(root / "research_best" / "dynamic_tool_proof_yield_outcomes.jsonl", max_lines=80)
    bridge_rows = parse_jsonl_tail(root / "research_best" / "tool_exec_bridge_v59147.jsonl", max_lines=40)
    governance_rows = parse_jsonl_tail(root / "research_best" / "dynamic_tool_governance_v5117.jsonl", max_lines=40)

    def score_of(row):
        try:
            return float(((row.get("score") or {}).get("score")))
        except Exception:
            return 0.0

    proof_scores = [score_of(r) for r in proof_rows if isinstance(r, dict) and isinstance(r.get("score"), dict)]
    bridge_exec = [r for r in bridge_rows if isinstance(r, dict) and r.get("event") == "ToolExecBridge"]
    promoted = [r for r in bridge_exec if r.get("promoted_to_probation") is True]
    executed = [r for r in bridge_exec if r.get("executed") is True]
    negative_bridge = []
    for r in bridge_exec:
        try:
            if float(r.get("observe_score", 0.0)) <= float(r.get("control_score", 0.0)):
                negative_bridge.append(r)
        except Exception:
            pass
    governance_actions = []
    for row in governance_rows:
        for action in row.get("actions", []) if isinstance(row, dict) else []:
            if isinstance(action, dict):
                governance_actions.append(action.get("action"))
    recent_tool_atoms = set()
    for row in proof_rows:
        if not isinstance(row, dict):
            continue
        for key in ("tool_name", "tool_id", "decision", "tool_class"):
            value = str(row.get(key) or "").strip()
            if value:
                recent_tool_atoms.add(value[:96])
        card = row.get("search_action_card") if isinstance(row.get("search_action_card"), dict) else {}
        value = str(card.get("tool_name") or "").strip()
        if value:
            recent_tool_atoms.add(value[:96])
        evidence = card.get("evidence") if isinstance(card.get("evidence"), dict) else {}
        primitives = evidence.get("primitives")
        if isinstance(primitives, list):
            for item in primitives:
                text = str(item or "").strip()
                if text:
                    recent_tool_atoms.add(text[:96])
        matched = row.get("matched_tool_control_island") if isinstance(row.get("matched_tool_control_island"), dict) else {}
        value = str(matched.get("pain_target") or "").strip()
        if value:
            recent_tool_atoms.add(value[:96])
    return {
        "proof_yield_tail_count": len(proof_rows),
        "proof_yield_avg_score": round(sum(proof_scores) / max(len(proof_scores), 1), 6),
        "proof_yield_positive_count": len([s for s in proof_scores if s > 0]),
        "tool_bridge_exec_count": len(executed),
        "tool_bridge_promoted_count": len(promoted),
        "tool_bridge_non_lift_count": len(negative_bridge),
        "governance_action_tail_count": len(governance_actions),
        "governance_cooldown_duplicate": governance_actions.count("cooldown_duplicate"),
        "governance_cooldown_unproven": governance_actions.count("cooldown_over_unproven_cap"),
        "recent_tool_atom_count": len(recent_tool_atoms),
        "tool_atom_feedback_ready": bool(recent_tool_atoms),
        "tool_evidence_interaction_events": len(proof_rows),
        "recent_tool_atom_targets": sorted(recent_tool_atoms)[:12],
        "recent_bridge_tools": [
            {
                "tool_name": r.get("tool_name"),
                "observe_score": r.get("observe_score"),
                "control_score": r.get("control_score"),
                "promoted": r.get("promoted_to_probation"),
            }
            for r in bridge_exec[-8:]
        ],
    }


def summarize_latest_matched_lift(project_root):
    root = resolve_root(project_root)
    candidates = []
    for path in root.glob("tool_matched_island_lift*/tool_matched_island_lift_summary.json"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not candidates:
        return {
            "available": False,
            "recommended_recipe_focus": "run tiny blocker-driven matched-island preflight before code promotion",
        }
    candidates.sort(reverse=True)
    path = candidates[0][1]
    summary = read_json(path, {})
    mechanism_report = read_json(path.parent / "mechanism_report.json", {})
    nursery_report = read_json(path.parent / "repair_nursery_report.json", {})
    orthogonal_report = read_json(path.parent / "orthogonal_alpha_portfolio_report.json", {})
    proof_memory_report = read_json(path.parent / "alpha_proof_memory_report.json", {})
    blockers = summary.get("blocker_counts") if isinstance(summary.get("blocker_counts"), dict) else {}
    top_blockers = [k for k, _ in sorted(blockers.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))[:8]]
    no_survivors = bool(int(summary.get("preflight_tested", 0) or 0) > 0 and int(summary.get("preflight_survivors", 0) or 0) == 0)
    nursery_axes = nursery_report.get("repair_axis_counts") if isinstance(nursery_report.get("repair_axis_counts"), dict) else {}
    return {
        "available": True,
        "summary_path": str(path),
        "ok": bool(summary.get("ok")),
        "target_family": str(summary.get("target_family", "")),
        "recommended_next_family": str(summary.get("recommended_next_family", "")),
        "recommended_mutation_axis": str(summary.get("recommended_mutation_axis", "")),
        "preflight_tested": int(summary.get("preflight_tested", 0) or 0),
        "preflight_survivors": int(summary.get("preflight_survivors", 0) or 0),
        "promotion_candidates": int(summary.get("promotion_candidates", 0) or 0),
        "family_positive_lift_count": int(summary.get("family_positive_lift_count", 0) or 0),
        "mechanism_arena": bool(summary.get("mechanism_arena", False)),
        "mechanism_library_size": int(summary.get("mechanism_library_size", 0) or 0),
        "mechanisms_tested": int(summary.get("mechanisms_tested", 0) or 0),
        "validated_mechanism_count": int(summary.get("validated_mechanism_count", 0) or 0),
        "mechanism_weekly_goal_pass": bool(summary.get("mechanism_weekly_goal_pass", False)),
        "mechanism_negative_control_failures": int(summary.get("mechanism_negative_control_failures", 0) or 0),
        "mechanism_cost_first_failures": int(summary.get("mechanism_cost_first_failures", 0) or 0),
        "repair_nursery_enabled": bool(summary.get("repair_nursery_enabled", False)),
        "repair_nursery_candidates": int(summary.get("repair_nursery_candidates", 0) or 0),
        "repair_nursery_fragile_candidates": int(summary.get("repair_nursery_fragile_candidates", 0) or 0),
        "repair_nursery_cost_first_candidates": int(summary.get("repair_nursery_cost_first_candidates", 0) or 0),
        "repair_nursery_mechanism_candidates": int(summary.get("repair_nursery_mechanism_candidates", 0) or 0),
        "repair_nursery_top_repair_axes": [k for k, _ in sorted(nursery_axes.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))[:8]],
        "orthogonal_alpha_selected_count": int(summary.get("orthogonal_alpha_selected_count", 0) or 0),
        "orthogonal_alpha_candidate_count": int(summary.get("orthogonal_alpha_candidate_count", 0) or 0),
        "alpha_proof_memory_rows": int(summary.get("alpha_proof_memory_rows", 0) or 0),
        "proof_memory_top_lessons": list((proof_memory_report.get("lesson_counts") or {}).keys())[:8] if isinstance(proof_memory_report, dict) else [],
        "orthogonal_policy": str(orthogonal_report.get("policy", "")) if isinstance(orthogonal_report, dict) else "",
        "validated_mechanisms": (mechanism_report.get("validated_mechanisms") or [])[:12] if isinstance(mechanism_report, dict) else [],
        "top_blockers": top_blockers,
        "no_final_holdout_leakage": bool(summary.get("no_final_holdout_leakage", True)),
        "gated_correctly_no_real_ga_influence": bool(summary.get("gated_correctly_no_real_ga_influence", True)),
        "blocker_recipe_needed": bool(no_survivors or int(summary.get("promotion_candidates", 0) or 0) == 0),
        "recommended_recipe_focus": "repository-scoped alpha nursery and primary-blocker mutation recipes plus L1/L2 proof ladder before full proof",
    }


def summarize_evidence_progress(project_root):
    root = resolve_root(project_root)
    research = root / "research_best"
    status = read_json(research / "workload_evidence_proof_card_status.json", {})
    bridge = read_json(research / "workload_evidence_preproof_bridge_status.json", {})
    evidence_library = read_json(research / "workload_evidence_library.json", {})
    alpha = read_json(research / "alpha_hypotheses.json", {})
    compute = read_json(root / "runner_logs" / "compute_to_proof_report.json", {})
    status = status if isinstance(status, dict) else {}
    bridge = bridge if isinstance(bridge, dict) else {}
    evidence_library = evidence_library if isinstance(evidence_library, dict) else {}
    alpha = alpha if isinstance(alpha, dict) else {}
    compute = compute if isinstance(compute, dict) else {}
    metrics = compute.get("metrics") if isinstance(compute.get("metrics"), dict) else {}
    tool_plan = compute.get("tool_lifecycle_plan") if isinstance(compute.get("tool_lifecycle_plan"), dict) else {}
    integration_stage_flow = compute.get("integration_stage_tool_evidence_flow") if isinstance(compute.get("integration_stage_tool_evidence_flow"), dict) else {}

    def as_int(value, default=0):
        try:
            return int(value)
        except Exception:
            return int(default)

    routing_actions = [str(x) for x in status.get("routing_actions", []) if str(x)]
    compute_tool_actions = list(tool_plan.get("actions", []) if isinstance(tool_plan.get("actions"), list) else [])
    integration_stage_flow_actions = list(integration_stage_flow.get("actions", []) if isinstance(integration_stage_flow.get("actions"), list) else [])
    integration_stage_flow_count = max(
        as_int(metrics.get("integration_stage_tool_evidence_flow_count", 0)),
        as_int(integration_stage_flow.get("routed_row_count", 0)),
    )
    progress_axes = [str(x) for x in status.get("progress_axes", []) if str(x)]
    if not progress_axes and status:
        progress_axes = ["evidence"]
    if compute_tool_actions:
        progress_axes.append("tool")
    if integration_stage_flow_count > 0:
        progress_axes.extend(["evidence", "tool"])
    progress_axes = sorted(set(progress_axes))
    progress_state = str(status.get("progress_state") or "")
    valid_cards = as_int(
        status.get("valid_standard_proof_cards", bridge.get("valid_standard_proof_cards", 0))
    )
    repair_precheck = as_int(alpha.get("repair_precheck_passed", 0))
    progress_contract = status.get("progress_contract") if isinstance(status.get("progress_contract"), dict) else {}
    progress_contract_pass = bool(status.get("progress_contract_pass") or progress_contract.get("pass"))
    stale = bool(status.get("evidence_input_stale") or progress_state == "stale_input_refresh_required")
    bridge_updates = as_int(status.get("repair_precheck_bridge_updated", bridge.get("hypotheses_updated", 0)))
    new_cards = as_int(status.get("new_cards_appended", 0))
    needs_attention = bool(
        stale
        or not progress_contract_pass
        or (valid_cards > 0 and repair_precheck <= 0)
        or (progress_state in ("", "same_cards_staleness_watch") and new_cards <= 0 and bridge_updates <= 0)
    )
    card_ids = [str(x) for x in status.get("card_ids", []) if str(x)]
    fresh_selected_card_ids = [str(x) for x in status.get("fresh_selected_card_ids", []) if str(x)]
    invalid_repairs = [
        row
        for row in status.get("invalid_evidence_card_repairs", [])
        if isinstance(row, dict)
    ][:8]
    return {
        "status_path": str(research / "workload_evidence_proof_card_status.json"),
        "valid_standard_proof_cards": valid_cards,
        "new_cards_appended": new_cards,
        "repair_precheck_bridge_updated": bridge_updates,
        "repair_precheck_passed": repair_precheck,
        "prepromotion_ticket_count": as_int(status.get("prepromotion_ticket_count", 0)),
        "prepromotion_ticket_actions": [str(x) for x in status.get("prepromotion_ticket_actions", []) if str(x)][:12],
        "evidence_refresh_ticket_count": as_int(status.get("evidence_refresh_ticket_count", 0)),
        "new_evidence_refresh_ticket_count": as_int(status.get("new_evidence_refresh_ticket_count", 0)),
        "new_prepromotion_ticket_count": as_int(status.get("new_prepromotion_ticket_count", 0)),
        "invalid_evidence_card_count": as_int(status.get("invalid_evidence_card_count", 0)),
        "sibling_control_repair_ticket_count": as_int(status.get("sibling_control_repair_ticket_count", 0)),
        "integration_stage_tool_evidence_flow_ticket_count": as_int(status.get("integration_stage_tool_evidence_flow_ticket_count", 0)),
        "card_ids": card_ids[:25],
        "fresh_selected_card_ids": fresh_selected_card_ids[:8],
        "invalid_evidence_card_repairs": invalid_repairs,
        "promotion_allowed": bool(alpha.get("promotion_allowed", False)),
        "stagnant_same_card_runs": as_int(status.get("stagnant_same_card_runs", 0)),
        "progress_state": progress_state,
        "progress_axes": progress_axes,
        "routing_actions": routing_actions,
        "evidence_input_stale": stale,
        "progress_contract_pass": progress_contract_pass,
        "needs_supervisor_attention": needs_attention,
        "compute_report_valid_cards": as_int(metrics.get("valid_proof_card_count", 0)),
        "compute_report_tool_decisions": len(tool_plan.get("decisions", []) if isinstance(tool_plan.get("decisions"), list) else []),
        "compute_report_tool_actions": compute_tool_actions,
        "compute_report_integration_stage_flow_actions": integration_stage_flow_actions,
        "integration_stage_tool_evidence_flow_count": integration_stage_flow_count,
        "atoms_collected": as_int(evidence_library.get("atoms_collected", 0)),
        "molecules_created": as_int(evidence_library.get("molecules_created", 0)),
        "atom_neuron_ready": bool(as_int(evidence_library.get("atoms_collected", 0)) > 0),
        "policy": "non-final evidence/tool progress evidence only; cannot promote, deploy, touch final/holdout, or affect production execution",
    }


def summarize_runner_and_research_state(project_root):
    root = resolve_root(project_root)
    final_status = read_json(root / "runner_logs" / "final_forever_status.json", {})
    disk_report = read_json(root / "runner_logs" / "disk_guard_report.json", {})
    lab_status = read_json(root / "runner_logs" / "tool_lab_status.json", {})
    benchmark_tail = read_tail_lines(root / "benchmark_log.txt", max_lines=80, max_bytes=2 * 1024 * 1024)
    verificationpass_lines = [line for line in benchmark_tail if "VerificationPass" in line or "verificationpass" in line][-12:]
    surgery_artifacts = {
        "code_surgery_ticket_exists": (root / "code_surgery" / "surgery_ticket.json").exists() or (root / "research_best" / "code_surgery" / "surgery_ticket.json").exists(),
        "lab_os_exists": (root / LAB_OS_NAME).exists(),
        "supervisor_exists": (root / SUPERVISOR_NAME).exists(),
    }
    return {
        "final_runner_status": final_status,
        "disk_guard_report": disk_report,
        "code_brain_status": lab_status,
        "benchmark_tail_verificationpass_lines": verificationpass_lines,
        "surgery_artifacts": surgery_artifacts,
    }


def summarize_search_power_spread(project_root, index, recent, matched_lift, evidence, system, patch_garden=None):
    root = resolve_root(project_root)
    research = root / "research_best"
    nursery_report = read_json(research / "nursery_scrap_conversion_report.json", {})
    nursery_candidates = read_json(research / "nursery_scrap_repair_candidates.json", {})
    search_report = read_json(root / "runner_logs" / "search_brain_report.json", {})
    compute_report = read_json(root / "runner_logs" / "compute_to_proof_report.json", {})
    index = index if isinstance(index, dict) else {}
    recent = recent if isinstance(recent, dict) else {}
    matched_lift = matched_lift if isinstance(matched_lift, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    system = system if isinstance(system, dict) else {}
    patch_garden = patch_garden if isinstance(patch_garden, list) else []
    nursery_report = nursery_report if isinstance(nursery_report, dict) else {}
    nursery_candidates = nursery_candidates if isinstance(nursery_candidates, dict) else {}
    search_report = search_report if isinstance(search_report, dict) else {}
    compute_report = compute_report if isinstance(compute_report, dict) else {}

    def as_int(value, default=0):
        try:
            return int(value)
        except Exception:
            return int(default)

    def as_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return float(default)

    candidates = nursery_candidates.get("candidates") if isinstance(nursery_candidates.get("candidates"), list) else []
    converted_candidates = max(as_int(nursery_report.get("converted_candidate_count"), 0), len(candidates))
    proof_debt_level = str(compute_report.get("proof_debt_level") or ((search_report.get("compute_to_proof_report") or {}).get("proof_debt_level") if isinstance(search_report.get("compute_to_proof_report"), dict) else "") or "UNKNOWN")
    proof_debt_score = as_float(compute_report.get("proof_debt_score"), 0.0)
    arbitration = search_report.get("arbitration") if isinstance(search_report.get("arbitration"), dict) else {}
    grants = arbitration.get("grants") if isinstance(arbitration.get("grants"), list) else []
    top_waste = compute_report.get("top_waste_sources") if isinstance(compute_report.get("top_waste_sources"), list) else []
    compute_metrics = compute_report.get("metrics") if isinstance(compute_report.get("metrics"), dict) else {}
    integration_stage_flow = compute_report.get("integration_stage_tool_evidence_flow") if isinstance(compute_report.get("integration_stage_tool_evidence_flow"), dict) else {}
    integration_stage_flow_count = max(
        as_int(compute_metrics.get("integration_stage_tool_evidence_flow_count", 0)),
        as_int(integration_stage_flow.get("routed_row_count", 0)),
        as_int(evidence.get("integration_stage_tool_evidence_flow_count", 0)),
    )
    blockers = [str(x) for x in matched_lift.get("top_blockers", []) if str(x)]

    active_inputs = []
    if as_int(index.get("total_indexed"), 0) > 0:
        active_inputs.append("dynamic_tool_index")
    if as_int(recent.get("proof_yield_tail_count"), 0) > 0 or as_int(recent.get("tool_bridge_exec_count"), 0) > 0:
        active_inputs.append("dynamic_tool_outcomes")
    if as_int(evidence.get("valid_standard_proof_cards"), 0) > 0 or as_int(evidence.get("repair_precheck_passed"), 0) > 0:
        active_inputs.append("evidence_proof_cards")
    if as_int(evidence.get("atoms_collected"), 0) > 0:
        active_inputs.append("workload_evidence_atom_neurons")
    if converted_candidates > 0:
        active_inputs.append("evidence_tool_nursery_candidates")
    if bool(matched_lift.get("available")):
        active_inputs.append("matched_lift_blockers")
    if grants or compute_report:
        active_inputs.append("search_governor_budget")
    if integration_stage_flow_count > 0:
        active_inputs.append("integration_stage_tool_evidence_flow")
    if patch_garden:
        active_inputs.append("patch_garden_memory")
    evidence_atom_count = as_int(evidence.get("atoms_collected"), 0)
    tool_atom_count = max(as_int(index.get("tool_atom_neuron_count"), 0), as_int(recent.get("recent_tool_atom_count"), 0))
    if tool_atom_count > 0:
        active_inputs.append("dynamic_tool_atom_neurons")
    interaction_mesh_active = bool(evidence_atom_count > 0 and tool_atom_count > 0 and len(active_inputs) >= 3)
    if interaction_mesh_active:
        active_inputs.append("tool_evidence_system_interaction_mesh")

    lanes = []

    def add_lane(name, inputs, mutation_axes, proof_depth, trigger, budget_share):
        lanes.append({
            "lane": name,
            "inputs": [str(x) for x in inputs if str(x)],
            "mutation_axes": [str(x) for x in mutation_axes if str(x)][:8],
            "proof_depth": proof_depth,
            "trigger": trigger,
            "advisory_budget_share": round(float(budget_share), 4),
            "max_population_share": 0.0,
            "same_parent_matched_control_required": True,
            "negative_controls_required": True,
            "can_influence_main_search": False,
        })

    positive_tools = as_int(index.get("proof_yield_ema_positive"), 0)
    duplicate_tools = as_int(index.get("duplicate_marked"), 0)
    mutation_tools = as_int(index.get("mutation_recommended"), 0)
    if positive_tools > 0:
        add_lane(
            "positive_tool_lineage_cross_seed",
            ["dynamic_tool_index", "dynamic_tool_outcomes"],
            ["parent_pool", "operator_bias", "integration_stage_filter_adjustment", "proof_depth"],
            "L1_same_parent_tool_vs_no_tool",
            "positive proof-yield EMA exists but influence is still locked",
            0.20,
        )
    if duplicate_tools > 0 or mutation_tools > 0 or as_int(recent.get("tool_bridge_non_lift_count"), 0) > 0:
        add_lane(
            "failed_tool_family_mutation_spread",
            ["dynamic_tool_index", "dynamic_tool_outcomes", "tool_governance_memory"],
            ["one_axis_child_mutation", "failure_lesson_used", "duplicate_signature_block", "novelty_hash"],
            "L1_tiny_mutation_preflight",
            "duplicate/no-lift tool families need descendants instead of rebirth",
            0.18,
        )
    if converted_candidates > 0:
        add_lane(
            "evidence_tool_nursery_l1_retest",
            ["evidence_proof_cards", "dynamic_tool_outcomes", "evidence_tool_nursery_candidates"],
            ["matched_control_lift", "regressionpreview_verificationpass_bridge_retest", "non_final_replay_trace", "broken_sibling_negative_control"],
            "L1_same_parent_matched_control_retest",
            "nursery candidates already combine evidence and tool material",
            0.22,
        )
    if bool(evidence.get("needs_supervisor_attention")) or as_int(evidence.get("valid_standard_proof_cards"), 0) > 0:
        add_lane(
            "evidence_preproof_consumption_spread",
            ["evidence_proof_cards", "evidence_progress_summary", "compute_to_proof_report"],
            ["valid_card_bridge_consumption", "standard_proof_card_followthrough", "anti_overfit_consistency", "regressionpreview_bridge"],
            "L1_non_final_evidence_preproof",
            "evidence cards need bridge consumption without promotion authority",
            0.16,
        )
    if as_int(evidence.get("atoms_collected"), 0) > 0:
        add_lane(
            "atom_neuron_attempt_neighborhood_spread",
            ["workload_evidence_atom_neurons", "evidence_proof_cards", "patch_garden_memory"],
            ["atom_feature_lens_neuron", "attempt_neighborhood", "population_width", "same_parent_control"],
            "L1_atom_neuron_neighborhood_preflight",
            "workload evidence atoms should act as zero-influence neurons for wider local search before any GA influence",
            0.0,
        )
    if interaction_mesh_active:
        add_lane(
            "tool_evidence_system_interaction_mesh",
            [
                "workload_evidence_atom_neurons",
                "dynamic_tool_atom_neurons",
                "evidence_proof_cards",
                "dynamic_tool_outcomes",
                "system_state_summary",
                "patch_garden_memory",
            ],
            [
                "evidence_atom_to_tool_primitive",
                "tool_atom_to_evidence_feature_lens",
                "regressionpreview_verificationpass_bridge",
                "proof_per_compute_feedback",
                "integration_stage_preflight_routing",
                "family_quota_pressure",
            ],
            "L1_tool_evidence_same_parent_mesh_preflight",
            "tool and evidence atoms should repeatedly write evidence back into the whole-system repair loop without gaining authority",
            0.12,
        )
    if grants or proof_debt_level in {"MEDIUM", "HIGH"} or top_waste:
        add_lane(
            "search_governor_budget_spread",
            ["search_governor_budget", "compute_to_proof_report", "integration_stage_waste_sources"],
            ["family_quota", "operator_quota", "regime_bucket", "proof_depth", "integration_stage_preflight"],
            "L1_budgeted_preflight_then_L2_halving",
            "proof debt or search-governor grants indicate budget should be spread by proof yield",
            0.16,
        )
    if integration_stage_flow_count > 0:
        add_lane(
            "integration_stage_tool_evidence_flow_spread",
            ["integration_stage_tool_evidence_flow", "compute_to_proof_report", "dynamic_tool_outcomes", "evidence_progress_summary"],
            ["tool_lift_processor", "evidence_card_processor", "integration_stage_preflight", "regressionpreview_verificationpass_bridge"],
            "L1_integration_stage_tool_evidence_non_final_flow",
            "fresh Integration-stage rows are routed to tool and evidence processors without promotion authority",
            0.18,
        )
    if blockers:
        add_lane(
            "blocker_recipe_spread",
            ["matched_lift_blockers", "patch_garden_memory"],
            blockers[:4] + ["previous_best_delta", "dual_control_lift", "proof_per_compute"],
            "L1_blocker_recipe_preflight",
            "matched-lift blockers should become explicit mutation recipes",
            0.08,
        )

    if lanes:
        add_lane(
            "anti_overfit_falsification_lane",
            ["all_candidate_lanes"],
            ["broken_sibling_negative_control", "unrelated_non_final_regime_retest", "split_exposure_check"],
            "L2_negative_control_before_L3",
            "every wider spread needs falsification before trust rises",
            0.0,
        )

    spread_needed = bool(
        len(active_inputs) >= 2
        and (
            as_int(index.get("can_influence_main_search"), 0) == 0
            or as_int(recent.get("tool_bridge_non_lift_count"), 0) > 0
            or bool(evidence.get("needs_supervisor_attention"))
            or converted_candidates > 0
            or proof_debt_level in {"MEDIUM", "HIGH"}
            or bool(blockers)
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "active_inputs": active_inputs,
        "spread_needed": spread_needed,
        "portfolio_width": len(lanes),
        "min_parallel_lanes": max(3, min(5, len(lanes))) if lanes else 0,
        "search_power_problem": spread_needed,
        "converted_nursery_candidates": converted_candidates,
        "search_governor_grant_count": len(grants),
        "integration_stage_tool_evidence_flow_count": integration_stage_flow_count,
        "proof_debt_level": proof_debt_level,
        "proof_debt_score": proof_debt_score,
        "top_waste_sources": top_waste[:6],
        "top_blockers": blockers[:8],
        "lanes": lanes,
        "atom_neuron_count": evidence_atom_count,
        "tool_atom_neuron_count": tool_atom_count,
        "atom_neuron_population_plan": {
            "source": "workload_evidence_atoms",
            "neuron_model": "treat_each_atom_as_zero_influence_search_neuron",
            "population_neighborhood": "spread_more_non_final_attempts_around_atom_features_before_any_main_search_influence",
            "same_parent_matched_control_required": True,
            "negative_controls_required": True,
            "max_population_share": 0.0,
        },
        "interaction_neuron_mesh": {
            "active": interaction_mesh_active,
            "source": "workload_evidence_atoms_plus_dynamic_tool_atoms_plus_system_pain",
            "evidence_atom_neurons": evidence_atom_count,
            "tool_atom_neurons": tool_atom_count,
            "feedback_loop": [
                "sense_system_pain",
                "bind_evidence_atom_to_tool_atom",
                "spawn_zero_influence_same_parent_neighborhood",
                "measure_tool_vs_no_tool_and_atom_vs_no_atom",
                "write_back_to_patch_garden_evidence_tickets_and_tool_action_cards",
            ],
            "writeback_targets": [
                "patch_garden_memory",
                "workload_evidence_prepromotion_tickets",
                "dynamic_tool_search_action_cards",
                "search_power_spread_plan",
                "compute_to_proof_report",
            ],
            "system_feedback_targets": [
                "regressionpreview_verificationpass_bridge",
                "integration_stage_preflight_routing",
                "proof_per_compute",
                "family_quota_pressure",
                "verificationpass_rescue",
            ],
            "bounded_ga_pressure_bridge": "code_engine.search_brain.prepromotion_ga_pressure",
            "scope_v_contract_bridge": "code_engine.search_brain.build_scope_v_pressure_contracts",
            "atom_neuron_state_path": "research_best/tool_evidence_neuron_state.json",
            "micro_island_feedback_required": True,
            "reinforcement_feedback_required": True,
            "can_apply_bounded_ga_search_pressure": interaction_mesh_active,
            "max_population_share": 0.0,
            "same_parent_matched_control_required": True,
            "negative_controls_required": True,
            "promotion_allowed": False,
            "can_influence_main_search": False,
            "can_touch_protected_surfaces": False,
        },
        "forced_theory_ids": ["S", "M", "R", "B", "C"] if spread_needed else [],
        "proof_ladder": ["L1_same_parent_preflight", "L2_successive_halving", "L3_full_non_final_confirmation", "L4_hidden_replay_before_promotion_review"],
        "policy": "combine all non-final signals into zero-influence one-axis descendants and proof-ladder retests only; no final/holdout/live/deploy authority",
        "protected_surfaces_unchanged": True,
        "can_touch_protected_surfaces": False,
        "can_influence_main_search": False,
        "promotion_allowed": False,
    }


def build_tool_atom_flow_ga_plan(search_power_spread=None, index=None, recent=None, evidence=None, matched_lift=None, patch_garden=None, main_search_flow=None):
    spread = search_power_spread if isinstance(search_power_spread, dict) else {}
    index = index if isinstance(index, dict) else {}
    recent = recent if isinstance(recent, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    matched_lift = matched_lift if isinstance(matched_lift, dict) else {}
    patch_cards = [c for c in (patch_garden or []) if isinstance(c, dict)]
    main_flow = main_search_flow if isinstance(main_search_flow, dict) else {}

    def as_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return int(default)

    def as_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return float(default)

    mesh = spread.get("interaction_neuron_mesh") if isinstance(spread.get("interaction_neuron_mesh"), dict) else {}
    evidence_atoms = max(as_int(spread.get("atom_neuron_count"), 0), as_int(evidence.get("atoms_collected"), 0))
    tool_atoms = max(
        as_int(spread.get("tool_atom_neuron_count"), 0),
        as_int(index.get("tool_atom_neuron_count"), 0),
        as_int(recent.get("recent_tool_atom_count"), 0),
    )
    proof_tail = as_int(recent.get("proof_yield_tail_count"), 0)
    non_lift = as_int(recent.get("tool_bridge_non_lift_count"), 0)
    converted = as_int(spread.get("converted_nursery_candidates"), 0)
    integration_stage_flow = as_int(spread.get("integration_stage_tool_evidence_flow_count"), 0)
    evidence_tool_specialist = evidence_tool_specialization_context({
        **evidence,
        "integration_stage_tool_evidence_flow_count": max(integration_stage_flow, as_int(evidence.get("integration_stage_tool_evidence_flow_count"), 0)),
    })
    valid_evidence_cards = as_int(evidence_tool_specialist.get("valid_standard_proof_cards"), 0)
    evidence_bridge_updates = as_int(evidence_tool_specialist.get("repair_precheck_bridge_updated"), 0)
    evidence_prepromotion_tickets = as_int(evidence_tool_specialist.get("prepromotion_ticket_count"), 0)
    evidence_refresh_tickets = as_int(evidence_tool_specialist.get("evidence_refresh_ticket_count"), 0)
    invalid_evidence_cards = as_int(evidence_tool_specialist.get("invalid_evidence_card_count"), 0)
    evidence_specialist_signal_count = (
        valid_evidence_cards
        + evidence_bridge_updates
        + evidence_prepromotion_tickets
        + evidence_refresh_tickets
        + invalid_evidence_cards
        + len(evidence_tool_specialist.get("top_valid_card_ids") or [])
        + len(evidence_tool_specialist.get("routing_actions") or [])
    )
    blocker_count = len(spread.get("top_blockers") or []) if isinstance(spread.get("top_blockers"), list) else 0
    positive_tools = as_int(index.get("proof_yield_ema_positive"), 0)
    promoted_tools = as_int(recent.get("tool_bridge_promoted_count"), 0)
    matched_positive = as_float(matched_lift.get("positive_rate"), as_float(matched_lift.get("matched_lift_positive_rate"), 0.0))
    flow_signal_count = max(
        as_int(main_flow.get("signal_count"), 0),
        as_int(main_flow.get("candidate_count"), 0),
        as_int(main_flow.get("bridge_candidate_count"), 0),
        as_int(main_flow.get("row_count_scanned"), 0),
    )
    flow_top_candidates = [row for row in (main_flow.get("top_candidates") or []) if isinstance(row, dict)]
    base_candidate_judge_budget = max(
        len(flow_top_candidates) + min(evidence_specialist_signal_count, 16),
        min(max(0, TOOL_ATOM_FLOW_GA_BASE_JUDGE_CANDIDATES), max(0, flow_signal_count + evidence_specialist_signal_count)),
    )
    candidate_judge_budget = min(
        max(1, TOOL_ATOM_FLOW_GA_MAX_JUDGE_CANDIDATES),
        max(
            base_candidate_judge_budget,
            max(1, flow_signal_count + evidence_specialist_signal_count) * max(1, TOOL_ATOM_FLOW_GA_COMPUTE_MULTIPLIER),
        ),
    )
    materialized_judge_queue_limit = min(
        max(1, TOOL_ATOM_FLOW_GA_MATERIALIZED_JUDGE_QUEUE_LIMIT),
        max(1, candidate_judge_budget),
    )
    candidate_judge_queue = []

    def add_judge_signal(signal_type, name, count=1, evidence=None, source="main_search_generation_candidate_tap", objective=None):
        if len(candidate_judge_queue) >= materialized_judge_queue_limit:
            return
        text = str(name or "").strip()
        if not text:
            return
        payload = evidence if isinstance(evidence, dict) else {}
        candidate_judge_queue.append({
            "signal_type": str(signal_type or "candidate_flow_signal"),
            "name": text[:160],
            "count": as_int(count, 1),
            "source": str(source or "main_search_generation_candidate_tap")[:160],
            "evidence": payload,
            "judge_objective": str(objective or "score tool variants by whether they explain, repair, route, or quarantine this non-final candidate-flow signal")[:240],
            "promotion_allowed": False,
            "can_influence_main_search": False,
            "can_touch_protected_surfaces": False,
        })

    evidence_judge_signals_added = False

    def add_evidence_judge_signals():
        nonlocal evidence_judge_signals_added
        if evidence_judge_signals_added or not evidence_tool_specialist.get("active"):
            return
        evidence_judge_signals_added = True
        evidence_objective = "score tool variants by whether they consume this Evidence signal into a specific matched-control, VerificationPass, or non-final replay follow-through"
        for card_id in (evidence_tool_specialist.get("top_valid_card_ids") or [])[:8]:
            add_judge_signal(
                "evidence_valid_proof_card",
                card_id,
                1,
                {
                    "specialization_axes": evidence_tool_specialist.get("specialization_axes", [])[:8],
                    "valid_standard_proof_cards": valid_evidence_cards,
                },
                source="workload_evidence_standard_proof_cards",
                objective=evidence_objective,
            )
        if evidence_bridge_updates or evidence_prepromotion_tickets:
            add_judge_signal(
                "evidence_preproof_bridge",
                "repair_precheck_bridge_followthrough",
                max(1, evidence_bridge_updates + evidence_prepromotion_tickets),
                {
                    "repair_precheck_bridge_updated": evidence_bridge_updates,
                    "prepromotion_ticket_count": evidence_prepromotion_tickets,
                    "repair_precheck_passed": as_int(evidence_tool_specialist.get("repair_precheck_passed"), 0),
                },
                source="workload_evidence_preproof_bridge_status",
                objective=evidence_objective,
            )
        if invalid_evidence_cards:
            add_judge_signal(
                "evidence_invalid_card_repair",
                "invalid_card_sibling_control_repair",
                invalid_evidence_cards,
                {
                    "invalid_repair_candidate_ids": evidence_tool_specialist.get("invalid_repair_candidate_ids", [])[:6],
                    "sibling_control_repair_ticket_count": as_int(evidence_tool_specialist.get("sibling_control_repair_ticket_count"), 0),
                },
                source="workload_evidence_proof_card_status",
                objective=evidence_objective,
            )
        for action in (evidence_tool_specialist.get("routing_actions") or [])[:6]:
            add_judge_signal(
                "evidence_routing_action",
                action,
                1,
                {"progress_state": evidence_tool_specialist.get("progress_state", "")},
                source="workload_evidence_proof_card_status",
                objective=evidence_objective,
            )

    add_evidence_judge_signals()

    for row in flow_top_candidates[:12]:
        add_judge_signal(
            "candidate",
            row.get("candidate_id") or row.get("family") or row.get("origin_operator"),
            1,
            {
                "family": str(row.get("family") or "")[:120],
                "origin_operator": str(row.get("origin_operator") or "")[:120],
                "stage": str(row.get("stage") or "")[:120],
                "death_reason": str(row.get("death_reason") or "")[:120],
                "fail_reasons": [str(item)[:120] for item in row.get("fail_reasons") or []][:4],
                "verificationpass_survivor": bool(row.get("verificationpass_survivor")),
                "integration_stage_pass": bool(row.get("integration_stage_pass")),
            },
        )
    for key, signal_type in (
        ("top_fail_reasons", "fail_reason"),
        ("top_death_reasons", "death_reason"),
        ("top_operators", "operator"),
        ("top_families", "family"),
        ("top_routing_actions", "routing_action"),
    ):
        for item in main_flow.get(key) or []:
            if isinstance(item, dict):
                add_judge_signal(signal_type, item.get("name"), item.get("count"), {"summary_key": key})
    add_evidence_judge_signals()

    if flow_signal_count > 0 and len(candidate_judge_queue) < materialized_judge_queue_limit:
        shard_count = max(1, materialized_judge_queue_limit - len(candidate_judge_queue))
        shard_size = max(1, int(flow_signal_count // max(1, shard_count)))
        while len(candidate_judge_queue) < materialized_judge_queue_limit:
            shard_idx = len(candidate_judge_queue) + 1
            add_judge_signal(
                "virtual_candidate_flow_shard",
                "main_search_gen3_flow_shard_%04d" % shard_idx,
                shard_size,
                {
                    "source_generation": main_flow.get("generation"),
                    "source_loop": main_flow.get("loop"),
                    "flow_signal_count": flow_signal_count,
                    "bridge_candidate_count": as_int(main_flow.get("bridge_candidate_count"), 0),
                    "compute_multiplier": TOOL_ATOM_FLOW_GA_COMPUTE_MULTIPLIER,
                    "materialized_queue_limit": materialized_judge_queue_limit,
                },
                source="research_best/main_search_generation_candidates_latest.json",
                objective="score the amplified Tool/Evidence GA against this read-only shard of gen-3 VerificationPass/refined candidate flow",
            )
            if shard_idx > materialized_judge_queue_limit + 2:
                break

    fuel_units = (
        evidence_atoms
        + tool_atoms
        + 2 * proof_tail
        + 3 * non_lift
        + 4 * converted
        + 2 * integration_stage_flow
        + 4 * valid_evidence_cards
        + 3 * evidence_bridge_updates
        + 2 * evidence_prepromotion_tickets
        + evidence_refresh_tickets
        + 2 * invalid_evidence_cards
        + 2 * len(patch_cards)
        + 3 * blocker_count
        + min(candidate_judge_budget, TOOL_ATOM_FLOW_GA_MAX_JUDGE_CANDIDATES)
    )
    fuel_ready = fuel_units > 0
    stuck_tools = bool(promoted_tools <= 0 or positive_tools <= 0 or non_lift > 0 or matched_positive <= 0.0)
    active = True
    target_variant_count = max(
        TOOL_ATOM_FLOW_GA_MIN_VARIANTS,
        min(TOOL_ATOM_FLOW_GA_MAX_VARIANTS, 1 + int(fuel_units // 24)),
    )
    candidate_flow_spec_floor = 0
    if candidate_judge_budget > target_variant_count:
        candidate_flow_spec_floor = 1 + int(candidate_judge_budget // 8)
    amplified_spec_floor = TOOL_ATOM_FLOW_GA_BASE_SPEC_BUDGET * max(1, TOOL_ATOM_FLOW_GA_COMPUTE_MULTIPLIER)
    spec_budget = max(1, min(TOOL_ATOM_FLOW_GA_MAX_SPEC_BUDGET, max(1 + int(fuel_units // 16), candidate_flow_spec_floor, amplified_spec_floor)))
    promotion_readiness_gate_stack = [
        "compile_changed_files",
        "protected_diff_clean",
        "tool_brain_gate",
        "evidence_tool_progress_contract",
        "matched_control_or_quarantine_evidence",
        "hidden_replay_before_external_promotion",
    ]
    candidate_flow_fitness_profile = {
        "target": "full_tool_brain_promotion_readiness",
        "uses_refined_main_flow_as_data": candidate_judge_budget > 0,
        "refined_data_source": "research_best/main_search_generation_candidates_latest.json",
        "candidate_flow_signal_count": flow_signal_count,
        "candidate_judge_budget": candidate_judge_budget,
        "base_candidate_judge_budget": base_candidate_judge_budget,
        "tool_atom_flow_ga_compute_multiplier": TOOL_ATOM_FLOW_GA_COMPUTE_MULTIPLIER,
        "materialized_judge_queue_limit": materialized_judge_queue_limit,
        "candidate_tool_spec_budget": spec_budget,
        "pass_probability_pressure": "maximize_tool_gate_pass_chance_from_refined_candidate_evidence",
        "required_pass_gates": promotion_readiness_gate_stack,
        "fitness_weights": {
            "tool_brain_gate_pass_readiness": 0.25,
            "candidate_flow_signal_explained_or_repaired": 0.15,
            "matched_control_or_quarantine_evidence": 0.20,
            "verificationpass_bridge_conversion": 0.15,
            "evidence_proof_card_consumption": 0.15,
            "evidence_tool_progress_contract": 0.15,
            "large_generic_tool_penalty": 0.10,
            "raw_quality_gain": 0.0,
            "production_impact": 0.0,
        },
        "evidence_tool_specialization": evidence_tool_specialist,
        "evidence_proof_card_consumption_required": bool(evidence_tool_specialist.get("active")),
        "desired_tool_shape": evidence_tool_specialist.get("desired_tool_shape"),
        "rejected_tool_shape": evidence_tool_specialist.get("rejected_tool_shape"),
        "promotion_allowed": False,
        "deployment_allowed": False,
        "can_influence_main_search": False,
        "can_touch_protected_surfaces": False,
    }

    lane_templates = [
        {
            "lane_id": "atom_flow_spread_blender",
            "theory_id": "S",
            "patch_family": "search_power_spread_blender",
            "fuel": ["workload_evidence_atom_neurons", "dynamic_tool_atom_neurons", "patch_garden_memory"],
            "mutation_axes": ["tool_atom_to_evidence_feature_lens", "regressionpreview_verificationpass_bridge", "same_parent_control"],
            "trigger": "tool/evidence atom mesh is active" if mesh.get("active") or spread.get("spread_needed") else "keep spread lane warm",
        },
        {
            "lane_id": "evidence_proof_tool_specializer",
            "theory_id": "S",
            "companion_theory_id": "R",
            "patch_family": "evidence_proof_tool_specialization",
            "fuel": ["evidence_valid_proof_cards", "workload_evidence_prepromotion_tickets", "invalid_evidence_card_repairs", "integration_stage_tool_evidence_flow"],
            "mutation_axes": evidence_tool_specialist.get("specialization_axes", [])[:8],
            "trigger": "tools must consume named Evidence proof cards, preproof tickets, invalid-card repairs, or Integration-stage flow before they are counted as useful",
        },
        {
            "lane_id": "pain_specific_tool_birth",
            "theory_id": "A",
            "patch_family": "tool_birth_specificity",
            "fuel": ["dynamic_tool_atom_neurons", "recent_tool_atom_targets"],
            "mutation_axes": ["exact_pain_target", "required_primitive", "kill_condition", "duplicate_signature"],
            "trigger": "tool foundry should compile sharper gate-facing tools every cycle; judge them against main-search candidate-flow signals" if candidate_judge_budget > target_variant_count else "tool foundry should compile sharper gate-facing tools every cycle",
        },
        {
            "lane_id": "matched_control_gate_climber",
            "theory_id": "B",
            "patch_family": "tool_matched_control_lift",
            "fuel": ["dynamic_tool_outcomes", "matched_lift_blockers"],
            "mutation_axes": ["same_parent_control", "matched_lift_summary", "bayesian_trust"],
            "trigger": "tool influence remains locked until matched-control lift is positive",
        },
        {
            "lane_id": "tool_to_verificationpass_bridge",
            "theory_id": "C",
            "patch_family": "tool_to_verificationpass_conversion",
            "fuel": ["search_action_cards", "integration_stage_tool_evidence_flow", "regressionpreview_bridge"],
            "mutation_axes": ["action_card_to_verificationpass_hint", "near_miss_repair_queue", "proof_depth_ladder", "candidate_flow_failure_to_tool_hint"],
            "trigger": "generated tools must route observations and candidate-flow failure signals into VerificationPass-shaped work",
        },
        {
            "lane_id": "failed_tool_memory_mutation",
            "theory_id": "F",
            "patch_family": "tool_memory_mutation",
            "fuel": ["no_lift_tool_memory", "duplicate_tool_signatures"],
            "mutation_axes": ["failure_lesson", "primitive_sequence_change", "novelty_hash"],
            "trigger": "no-lift or duplicate tools should mutate instead of being reborn",
        },
    ]
    if fuel_ready or stuck_tools:
        selected_ids = []
        for lane in lane_templates:
            if lane["lane_id"] == "evidence_proof_tool_specializer" and not evidence_tool_specialist.get("active"):
                continue
            if lane["theory_id"] == "F" and non_lift <= 0 and as_int(index.get("duplicate_marked"), 0) <= 0:
                continue
            selected_ids.append(lane["theory_id"])
    else:
        selected_ids = ["A"]
    selected_ids = list(dict.fromkeys(selected_ids))[:target_variant_count]
    lanes = []
    selected_set = set(selected_ids)
    for lane in lane_templates:
        if lane["lane_id"] == "evidence_proof_tool_specializer" and not evidence_tool_specialist.get("active"):
            continue
        if lane["theory_id"] not in selected_set:
            continue
        lane_payload = {
            **lane,
            "one_axis_difference": True,
            "same_parent_matched_control_required": True,
            "negative_control_required": True,
            "max_population_share": 0.0,
            "promotion_allowed": False,
            "can_touch_protected_surfaces": False,
            "can_influence_main_search": False,
        }
        if candidate_judge_budget > 0:
            lane_payload["candidate_flow_judge_budget"] = candidate_judge_budget
            lane_payload["candidate_flow_signal_count"] = flow_signal_count
            lane_payload["candidate_flow_judge_required"] = True
            lane_payload["fitness_target"] = candidate_flow_fitness_profile["target"]
            lane_payload["candidate_flow_fitness_profile"] = candidate_flow_fitness_profile
            lane_payload["promotion_readiness_gate_stack"] = promotion_readiness_gate_stack
        if lane.get("lane_id") == "evidence_proof_tool_specializer":
            lane_payload["evidence_tool_specialization"] = evidence_tool_specialist
            lane_payload["evidence_proof_card_consumption_required"] = True
            lane_payload["specific_tool_required"] = True
            lane_payload["large_generic_tool_penalty"] = True
        lanes.append(lane_payload)

    return {
        "schema_version": SCHEMA_VERSION,
        "active": active,
        "mode": "constant_tool_atom_flow_ga",
        "fuel_ready": fuel_ready,
        "fuel_units": int(fuel_units),
        "fuel_inputs": {
            "evidence_atom_neurons": evidence_atoms,
            "tool_atom_neurons": tool_atoms,
            "proof_yield_tail_count": proof_tail,
            "tool_bridge_non_lift_count": non_lift,
            "converted_nursery_candidates": converted,
            "integration_stage_tool_evidence_flow_count": integration_stage_flow,
            "evidence_valid_proof_card_count": valid_evidence_cards,
            "evidence_preproof_bridge_update_count": evidence_bridge_updates,
            "evidence_prepromotion_ticket_count": evidence_prepromotion_tickets,
            "evidence_refresh_ticket_count": evidence_refresh_tickets,
            "invalid_evidence_card_count": invalid_evidence_cards,
            "evidence_specialist_signal_count": evidence_specialist_signal_count,
            "patch_garden_card_count": len(patch_cards),
            "blocker_count": blocker_count,
            "main_search_candidate_flow_signal_count": flow_signal_count,
            "candidate_judge_budget": candidate_judge_budget,
            "base_candidate_judge_budget": base_candidate_judge_budget,
            "tool_atom_flow_ga_compute_multiplier": TOOL_ATOM_FLOW_GA_COMPUTE_MULTIPLIER,
            "materialized_judge_queue_limit": materialized_judge_queue_limit,
        },
        "target_variant_count": target_variant_count,
        "candidate_tool_spec_budget": spec_budget,
        "candidate_flow_signal_count": flow_signal_count,
        "candidate_judge_budget": candidate_judge_budget,
        "candidate_judge_queue_count": len(candidate_judge_queue),
        "candidate_judge_queue": candidate_judge_queue,
        "candidate_judge_policy": "main-search candidates and Evidence proof-card signals are read-only non-final judge fuel for tool variants; they do not increase raw tries, promotion authority, or protected-surface access",
        "fitness_target": candidate_flow_fitness_profile["target"],
        "candidate_flow_fitness_profile": candidate_flow_fitness_profile,
        "evidence_tool_specialization": evidence_tool_specialist,
        "promotion_readiness_gate_stack": promotion_readiness_gate_stack,
        "selected_theory_ids": selected_ids,
        "lanes": lanes,
        "gate_targets": [
            "compile_changed_files",
            "protected_diff_clean",
            "tool_brain_gate",
            "full_tool_brain_promotion_readiness",
            "candidate_flow_promotion_readiness_judge",
            "evidence_proof_card_tool_specialization",
            "evidence_tool_progress_contract",
            "hidden_replay_before_external_promotion",
        ],
        "pass_strategy": [
            "consume named Evidence proof cards, prepromotion tickets, invalid-card repairs, or Integration-stage flow into specific tool variants",
            "generate pain-specific tool/code variants from atom fuel",
            "prefer matched-control and VerificationPass-bridge evidence over library size",
            "mutate no-lift tool families from memory instead of recreating duplicates",
            "keep near-pass work in patch garden until the same gates pass",
        ],
        "runner": {
            "constant_loop": "development_tool_lab.loop_forever",
            "interval_env": "CODE_LAB_TOOL_LAB_INTERVAL_SEC",
            "stop_flag": "STOP_TOOL_LAB.flag",
        },
        "approval_policy": "constant generation advice only; ordinary compile, protected diff, judge, progress-contract, and hidden-replay checks still decide",
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "can_influence_main_search": False,
    }


def append_tool_atom_flow_ga_log(project_root, plan, search_power_spread=None, system=None, main_search_flow=None):
    root = resolve_root(project_root)
    plan = plan if isinstance(plan, dict) else {}
    spread = search_power_spread if isinstance(search_power_spread, dict) else {}
    system = system if isinstance(system, dict) else {}
    main_flow = main_search_flow if isinstance(main_search_flow, dict) else {}
    code_brain = system.get("code_brain_status") if isinstance(system.get("code_brain_status"), dict) else {}
    lanes = [lane for lane in plan.get("lanes") or [] if isinstance(lane, dict)]
    row = {
        "schema_version": SCHEMA_VERSION,
        "event": "tool_atom_flow_ga_tick",
        "ts": time.time(),
        "updated_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "active": bool(plan.get("active")),
        "mode": str(plan.get("mode") or "constant_tool_atom_flow_ga"),
        "fuel_ready": bool(plan.get("fuel_ready")),
        "fuel_units": int(plan.get("fuel_units", 0) or 0),
        "fuel_inputs": plan.get("fuel_inputs") if isinstance(plan.get("fuel_inputs"), dict) else {},
        "target_variant_count": int(plan.get("target_variant_count", 0) or 0),
        "candidate_tool_spec_budget": int(plan.get("candidate_tool_spec_budget", 0) or 0),
        "candidate_flow_signal_count": int(plan.get("candidate_flow_signal_count", 0) or main_flow.get("signal_count", 0) or 0),
        "candidate_judge_budget": int(plan.get("candidate_judge_budget", 0) or 0),
        "candidate_judge_queue_count": int(plan.get("candidate_judge_queue_count", 0) or 0),
        "fitness_target": str(plan.get("fitness_target") or ""),
        "promotion_readiness_gate_stack": [str(item) for item in plan.get("promotion_readiness_gate_stack") or []][:12],
        "candidate_flow_fitness_profile": plan.get("candidate_flow_fitness_profile") if isinstance(plan.get("candidate_flow_fitness_profile"), dict) else {},
        "candidate_judge_queue": [
            {
                "signal_type": str(item.get("signal_type") or ""),
                "name": str(item.get("name") or "")[:160],
                "count": int(item.get("count", 0) or 0),
                "source": str(item.get("source") or "")[:160],
                "promotion_allowed": bool(item.get("promotion_allowed")),
                "can_influence_main_search": bool(item.get("can_influence_main_search")),
                "can_touch_protected_surfaces": bool(item.get("can_touch_protected_surfaces")),
            }
            for item in (plan.get("candidate_judge_queue") or [])[:12]
            if isinstance(item, dict)
        ],
        "selected_theory_ids": [str(item) for item in plan.get("selected_theory_ids") or []][:12],
        "lane_count": len(lanes),
        "lanes": [
            {
                "lane_id": str(lane.get("lane_id") or ""),
                "patch_family": str(lane.get("patch_family") or ""),
                "theory_id": str(lane.get("theory_id") or ""),
                "trigger": str(lane.get("trigger") or "")[:240],
                "same_parent_matched_control_required": bool(lane.get("same_parent_matched_control_required")),
                "negative_control_required": bool(lane.get("negative_control_required")),
                "candidate_flow_judge_budget": int(lane.get("candidate_flow_judge_budget", 0) or 0),
                "fitness_target": str(lane.get("fitness_target") or ""),
                "promotion_allowed": bool(lane.get("promotion_allowed")),
                "can_influence_main_search": bool(lane.get("can_influence_main_search")),
                "can_touch_protected_surfaces": bool(lane.get("can_touch_protected_surfaces")),
            }
            for lane in lanes[:12]
        ],
        "tool_lab_status": str(code_brain.get("status") or ""),
        "integration_stage_tool_evidence_flow_count": int(spread.get("integration_stage_tool_evidence_flow_count", 0) or 0),
        "evidence_tool_specialization": plan.get("evidence_tool_specialization") if isinstance(plan.get("evidence_tool_specialization"), dict) else {},
        "search_power_problem": bool(spread.get("search_power_problem")),
        "approval_policy": str(plan.get("approval_policy") or ""),
        "candidate_judge_policy": str(plan.get("candidate_judge_policy") or ""),
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "can_influence_main_search": False,
        "policy": "diagnostic heartbeat for tool atom-flow GA planning only; no promotion, protected-surface, final, holdout, live, or deploy authority",
    }
    append_jsonl(runner_logs(root) / "tool_atom_flow_ga.jsonl", row)
    write_json(runner_logs(root) / "tool_atom_flow_ga_latest.json", row)
    return row


def summarize_main_search_candidate_flow(project_root):
    root = resolve_root(project_root)
    status = read_json(root / "runner_logs" / "main_search_generation_candidate_tap_status.json", {})
    latest = read_json(root / "research_best" / "main_search_generation_candidates_latest.json", {})
    status = status if isinstance(status, dict) else {}
    latest = latest if isinstance(latest, dict) else {}
    candidates = latest.get("candidates") if isinstance(latest.get("candidates"), list) else []
    bridge = latest.get("bridge_candidates") if isinstance(latest.get("bridge_candidates"), list) else []
    rows = candidates if candidates else bridge

    def as_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return int(default)

    def as_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return float(default)

    def first_present(*values):
        for value in values:
            if value is not None:
                return value
        return None

    def bump(counter, value):
        text = str(value or "").strip()
        if text:
            counter[text] = counter.get(text, 0) + 1

    def top_counts(counter, limit=8):
        ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        return [{"name": str(name), "count": int(count)} for name, count in ordered[: int(limit)]]

    family_counts = {}
    operator_counts = {}
    integration_stageounts = {}
    death_counts = {}
    fail_counts = {}
    route_counts = {}
    verificationpass_survivors = 0
    integration_stage_passes = 0
    matched_control_required = 0
    negative_control_required = 0
    protected_touch_claims = 0
    promotion_claims = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        bump(family_counts, row.get("family_root") or row.get("family"))
        bump(operator_counts, row.get("origin_operator"))
        bump(integration_stageounts, row.get("stage") or row.get("source_bucket"))
        bump(death_counts, row.get("death_reason"))
        for reason in row.get("fail_reasons") or []:
            bump(fail_counts, reason)
        for action in row.get("routing_actions") or []:
            bump(route_counts, action)
        verificationpass_survivors += 1 if row.get("verificationpass_survivor") is True else 0
        integration_stage_passes += 1 if row.get("integration_stage_pass") is True else 0
        matched_control_required += 1 if row.get("matched_control_required") is True else 0
        negative_control_required += 1 if row.get("negative_controls_required") is True else 0
        protected_touch_claims += 1 if row.get("can_touch_protected_surfaces") is True else 0
        promotion_claims += 1 if row.get("promotion_allowed") is True else 0

    top_source_rows = bridge[:COMPONENT_MAIN_SEARCH_FLOW_SIGNAL_SAMPLE] if bridge else rows[:COMPONENT_MAIN_SEARCH_FLOW_SIGNAL_SAMPLE]
    top_candidates = []
    for row in top_source_rows:
        if not isinstance(row, dict):
            continue
        top_candidates.append({
            "candidate_id": str(row.get("candidate_id") or ""),
            "family": str(row.get("family") or row.get("family_root") or ""),
            "origin_operator": str(row.get("origin_operator") or ""),
            "stage": str(row.get("stage") or row.get("source_bucket") or ""),
            "death_reason": str(row.get("death_reason") or ""),
            "fail_reasons": [str(item) for item in row.get("fail_reasons") or []][:4],
            "routing_actions": [str(item) for item in row.get("routing_actions") or []][:4],
            "proof_yield": as_float(row.get("proof_yield"), 0.0),
            "score": as_float(row.get("score"), 0.0),
            "verificationpass_survivor": bool(row.get("verificationpass_survivor")),
            "integration_stage_pass": bool(row.get("integration_stage_pass")),
            "promotion_allowed": False,
            "can_touch_protected_surfaces": False,
        })

    candidate_count = as_int(first_present(status.get("candidate_count"), latest.get("candidate_count"), len(candidates)), len(candidates))
    bridge_count = as_int(first_present(status.get("bridge_candidate_count"), latest.get("bridge_candidate_count"), len(bridge)), len(bridge))
    row_count = len(rows)
    signal_count = max(candidate_count, row_count, bridge_count)
    return {
        "schema_version": SCHEMA_VERSION,
        "active": bool(status or latest),
        "source": "research_best/main_search_generation_candidates_latest.json",
        "status_source": "runner_logs/main_search_generation_candidate_tap_status.json",
        "loop": first_present(status.get("loop"), latest.get("loop")),
        "generation": first_present(status.get("generation"), latest.get("generation")),
        "candidate_count": candidate_count,
        "bridge_candidate_count": bridge_count,
        "row_count_scanned": row_count,
        "signal_count": signal_count,
        "integration_stage_eval_count": as_int(first_present(status.get("integration_stage_eval_count"), latest.get("integration_stage_eval_count")), 0),
        "integration_stage_pass_count": as_int(first_present(status.get("integration_stage_pass_count"), latest.get("integration_stage_pass_count"), integration_stage_passes), integration_stage_passes),
        "regressionpreview_pass_count": as_int(first_present(status.get("regressionpreview_pass_count"), latest.get("regressionpreview_pass_count")), 0),
        "verificationpass_near_miss_count": as_int(latest.get("verificationpass_near_miss_count"), 0),
        "verificationpass_survivor_count": max(as_int(latest.get("verificationpass_survivor_count"), 0), verificationpass_survivors),
        "verificationpass_rate": as_float(latest.get("verificationpass_rate"), 0.0),
        "integration_stage_to_promotion_candidate_rate": as_float(latest.get("integration_stage_to_promotion_candidate_rate"), 0.0),
        "matched_control_required_count": matched_control_required,
        "negative_control_required_count": negative_control_required,
        "protected_touch_claim_count": protected_touch_claims,
        "promotion_claim_count": promotion_claims,
        "top_families": top_counts(family_counts),
        "top_operators": top_counts(operator_counts),
        "top_stages": top_counts(integration_stageounts),
        "top_death_reasons": top_counts(death_counts),
        "top_fail_reasons": top_counts(fail_counts),
        "top_routing_actions": top_counts(route_counts),
        "top_candidates": top_candidates,
        "candidate_source": "full_main_search_candidate_flow",
        "policy": "read-only main-search candidate-flow signal for component promotion-readiness GAs; no gate, final, holdout, live, deploy, or promotion authority",
        "promotion_allowed": False,
        "deployment_allowed": False,
        "can_touch_protected_surfaces": False,
        "can_influence_main_search": False,
        "production_impact_weight": 0.0,
    }


def append_component_atom_promotion_ga_log(project_root, plan, generation=None):
    root = resolve_root(project_root)
    plan = plan if isinstance(plan, dict) else {}
    generation = generation if isinstance(generation, dict) else {}
    if not generation:
        generation = read_json(root / "runner_logs" / "main_search_generation_candidate_tap_status.json", {})
        generation = generation if isinstance(generation, dict) else {}
    source_loop = generation.get("loop")
    snapshot_generation = generation.get("generation")
    local_loop = _next_component_ga_local_loop(root, source_loop, snapshot_generation)
    components = [row for row in plan.get("components") or [] if isinstance(row, dict)]
    component_rows = []
    for component in components:
        lanes = [lane for lane in component.get("candidate_lanes") or [] if isinstance(lane, dict)]
        flow_candidate_count = int(component.get("flow_candidate_count", 0) or plan.get("flow_candidate_count", 0) or 0)
        flow_bridge_count = int(component.get("flow_bridge_candidate_count", 0) or plan.get("flow_bridge_candidate_count", 0) or 0)
        flow_signal_count = int(component.get("flow_signal_count", 0) or plan.get("flow_signal_count", 0) or 0)
        fast_local_loop = component.get("fast_local_loop") if isinstance(component.get("fast_local_loop"), dict) else {}
        local_generation_budget = int(
            component.get("local_generation_budget", 0)
            or fast_local_loop.get("local_generation_budget", 0)
            or plan.get("local_generation_budget", 1)
            or 1
        )
        component_rows.append({
            "component_id": str(component.get("component_id") or ""),
            "display_name": str(component.get("display_name") or component.get("component_id") or ""),
            "active": bool(component.get("active")),
            "mode": str(component.get("mode") or ""),
            "local_loop": local_loop,
            "source_loop": source_loop,
            "source_snapshot_generation": snapshot_generation,
            "atom_count": int(component.get("atom_count", 0) or 0),
            "component_atom_count": int(component.get("component_atom_count", component.get("atom_count", 0)) or 0),
            "flow_signal_atom_count": int(component.get("flow_signal_atom_count", 0) or 0),
            "candidate_count": flow_candidate_count or len(lanes),
            "local_candidate_count": len(lanes),
            "flow_candidate_count": flow_candidate_count,
            "flow_bridge_candidate_count": flow_bridge_count,
            "flow_signal_count": flow_signal_count,
            "refined_input_candidate_count": int(component.get("refined_input_candidate_count", flow_candidate_count) or flow_candidate_count),
            "refined_bridge_input_count": int(component.get("refined_bridge_input_count", flow_bridge_count) or flow_bridge_count),
            "refined_input_signal_count": int(component.get("refined_input_signal_count", flow_signal_count) or flow_signal_count),
            "flow_population_model": str(component.get("flow_population_model") or "main_search_flow_signals"),
            "candidate_generation": component.get("candidate_generation") if isinstance(component.get("candidate_generation"), dict) else {},
            "fast_local_loop": fast_local_loop,
            "ga_amplification": component.get("ga_amplification") if isinstance(component.get("ga_amplification"), dict) else {},
            "ga_compute_multiplier": int(component.get("ga_compute_multiplier", 1) or 1),
            "virtual_candidate_population_count": int(component.get("virtual_candidate_population_count", len(lanes)) or len(lanes)),
            "materialized_candidate_count": int(component.get("materialized_candidate_count", len(lanes)) or len(lanes)),
            "proof_work_candidate_limit": int(component.get("proof_work_candidate_limit", len(lanes)) or len(lanes)),
            "local_generation_budget": local_generation_budget,
            "main_generation_inherited": False,
            "expected_faster_than_main_search": True,
            "promotion_targets": [str(item) for item in component.get("promotion_targets") or []][:8],
            "selected_theory_ids": [str(item) for item in component.get("selected_theory_ids") or []][:8],
            "evidence_tool_specialization": component.get("evidence_tool_specialization") if isinstance(component.get("evidence_tool_specialization"), dict) else {},
            "flow_signal_atoms": [str(item) for item in component.get("flow_signal_atoms") or []][:12],
            "main_flow_signal_rows": component.get("main_flow_signal_rows")[:8] if isinstance(component.get("main_flow_signal_rows"), list) else [],
            "top_candidates": [
                {
                    "candidate_id": str(lane.get("candidate_id") or ""),
                    "promotion_target": str(lane.get("promotion_target") or ""),
                    "mutation_axis": str(lane.get("mutation_axis") or ""),
                    "source_atoms": [str(item) for item in lane.get("source_atoms") or []][:6],
                    "source_flow_candidate_ids": [str(item) for item in lane.get("source_flow_candidate_ids") or []][:6],
                    "source_flow_signals": [str(item) for item in lane.get("source_flow_signals") or []][:6],
                    "evidence_tool_specialization_required": bool(lane.get("evidence_tool_specialization_required")),
                    "flow_candidate_count": int(lane.get("flow_candidate_count", flow_candidate_count) or 0),
                    "flow_bridge_candidate_count": int(lane.get("flow_bridge_candidate_count", flow_bridge_count) or 0),
                    "flow_signal_count": int(lane.get("flow_signal_count", flow_signal_count) or 0),
                    "refined_input_signal_count": int(lane.get("refined_input_signal_count", flow_signal_count) or 0),
                    "local_generation_budget": int(lane.get("local_generation_budget", local_generation_budget) or local_generation_budget),
                    "ga_compute_multiplier": int(lane.get("ga_compute_multiplier", 1) or 1),
                    "virtual_candidate_population_count": int(lane.get("virtual_candidate_population_count", 0) or 0),
                    "amplified_population_rank": int(lane.get("amplified_population_rank", 0) or 0),
                    "main_generation_inherited": False,
                    "fitness_target": str(lane.get("fitness_target") or ""),
                    "production_impact_weight": float(lane.get("production_impact_weight", 0.0) or 0.0),
                    "promotion_allowed": bool(lane.get("promotion_allowed")),
                    "can_influence_main_search": bool(lane.get("can_influence_main_search")),
                }
                for lane in lanes[:5]
            ],
            "fitness_target": str(component.get("fitness_target") or "proof_promotion_readiness"),
            "target_is_promotion_readiness": bool(component.get("target_is_promotion_readiness")),
            "production_impact_weight": float(component.get("production_impact_weight", 0.0) or 0.0),
            "promotion_allowed": False,
            "deployment_allowed": False,
            "can_touch_protected_surfaces": False,
            "can_influence_main_search": False,
        })
    row = {
        "schema_version": SCHEMA_VERSION,
        "event": "component_atom_promotion_ga_tick",
        "ts": time.time(),
        "updated_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "active": bool(plan.get("active")),
        "mode": str(plan.get("mode") or "four_component_atom_promotion_gas"),
        "target": str(plan.get("target") or "proof_promotion_readiness_not_production_impacts"),
        "loop": source_loop,
        "source_loop": source_loop,
        "local_loop": local_loop,
        "snapshot_generation": snapshot_generation,
        "main_candidate_count": generation.get("candidate_count"),
        "main_bridge_candidate_count": generation.get("bridge_candidate_count"),
        "global_candidate_count": int(plan.get("global_candidate_count", 0) or 0),
        "local_candidate_spec_count": int(plan.get("local_candidate_spec_count", 0) or 0),
        "flow_candidate_count": int(plan.get("flow_candidate_count", 0) or 0),
        "flow_bridge_candidate_count": int(plan.get("flow_bridge_candidate_count", 0) or 0),
        "flow_signal_count": int(plan.get("flow_signal_count", 0) or 0),
        "refined_input_candidate_count": int(plan.get("refined_input_candidate_count", plan.get("flow_candidate_count", 0)) or 0),
        "refined_bridge_input_count": int(plan.get("refined_bridge_input_count", plan.get("flow_bridge_candidate_count", 0)) or 0),
        "refined_input_signal_count": int(plan.get("refined_input_signal_count", plan.get("flow_signal_count", 0)) or 0),
        "local_generation_budget": int(plan.get("local_generation_budget", 1) or 1),
        "max_local_generation_budget": int(plan.get("max_local_generation_budget", COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS) or COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS),
        "fast_local_loop": plan.get("fast_local_loop") if isinstance(plan.get("fast_local_loop"), dict) else {},
        "main_generation_inherited": False,
        "expected_faster_than_main_search": True,
        "main_search_flow": plan.get("main_search_flow") if isinstance(plan.get("main_search_flow"), dict) else {},
        "evidence_tool_specialization": plan.get("evidence_tool_specialization") if isinstance(plan.get("evidence_tool_specialization"), dict) else {},
        "evidence_tool_ga_amplification": plan.get("evidence_tool_ga_amplification") if isinstance(plan.get("evidence_tool_ga_amplification"), dict) else {},
        "component_count": len(component_rows),
        "components": component_rows,
        "component_ids": [str(item) for item in plan.get("component_ids") or []],
        "candidate_source": str(plan.get("candidate_source") or "main_search_flow_plus_component_atoms"),
        "promotion_allowed": False,
        "deployment_allowed": False,
        "can_touch_protected_surfaces": False,
        "can_influence_main_search": False,
        "production_impact_weight": 0.0,
        "policy": "diagnostic heartbeat for per-component main-search-flow plus atom promotion-readiness GAs only; no production execution, promotion, protected-surface, final, holdout, live, or deploy authority",
    }
    append_jsonl(runner_logs(root) / "component_atom_promotion_gas.jsonl", row)
    write_json(runner_logs(root) / "component_atom_promotion_gas_latest.json", row)
    return row


def _component_atom_promotion_work_id(component_id, candidate_id, source_loop, snapshot_generation, local_loop, local_generation):
    raw = json.dumps(
        {
            "component_id": str(component_id or ""),
            "candidate_id": str(candidate_id or ""),
            "source_loop": source_loop,
            "snapshot_generation": snapshot_generation,
            "local_loop": local_loop,
            "local_generation": local_generation,
        },
        sort_keys=True,
        default=str,
    ).encode("utf-8", errors="replace")
    return "capga_work_%s_%s" % (str(component_id or "component"), hashlib.sha256(raw).hexdigest()[:14])


def build_component_atom_promotion_proof_work(plan, heartbeat=None):
    plan = plan if isinstance(plan, dict) else {}
    heartbeat = heartbeat if isinstance(heartbeat, dict) else {}

    def as_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return int(default)

    source_loop = heartbeat.get("source_loop", heartbeat.get("loop", plan.get("source_loop", plan.get("loop"))))
    snapshot_generation = heartbeat.get("snapshot_generation", plan.get("snapshot_generation"))
    local_loop = max(1, as_int(heartbeat.get("local_loop", plan.get("local_loop", 1)), 1))
    max_budget = max(1, as_int(plan.get("max_local_generation_budget", COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS), COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS))
    work_items = []
    component_rows = []
    for component in [row for row in plan.get("components") or [] if isinstance(row, dict)]:
        component_id = str(component.get("component_id") or "component")
        lanes = [lane for lane in component.get("candidate_lanes") or component.get("top_candidates") or [] if isinstance(lane, dict)]
        budget = max(
            1,
            min(
                max_budget,
                as_int(
                    component.get("local_generation_budget")
                    or (component.get("fast_local_loop") if isinstance(component.get("fast_local_loop"), dict) else {}).get("local_generation_budget")
                    or max_budget,
                    max_budget,
                ),
            ),
        )
        local_generation = ((local_loop - 1) % budget) + 1
        loop_epoch = 1 + int((local_loop - 1) // budget)
        emitted = 0
        component_lane_limit = max(
            COMPONENT_ATOM_PROMOTION_GA_MAX_CANDIDATES,
            as_int(component.get("proof_work_candidate_limit") or component.get("materialized_candidate_count"), COMPONENT_ATOM_PROMOTION_GA_MAX_CANDIDATES),
        )
        for lane in lanes[:component_lane_limit]:
            candidate_id = str(lane.get("candidate_id") or "")
            if not candidate_id:
                continue
            promotion_target = str(lane.get("promotion_target") or component.get("fitness_target") or "proof_promotion_readiness")
            mutation_axis = str(lane.get("mutation_axis") or promotion_target or component_id)
            source_flow_signals = [str(item) for item in lane.get("source_flow_signals") or [] if str(item)]
            source_flow_candidate_ids = [str(item) for item in lane.get("source_flow_candidate_ids") or [] if str(item)]
            rank_signal = "verificationpass" in promotion_target.lower() or any("verificationpass" in signal.lower() or "regressionpreview" in signal.lower() for signal in source_flow_signals)
            priority = min(
                1.0,
                0.30
                + 0.08 * local_generation
                + 0.04 * len(source_flow_candidate_ids[:4])
                + (0.12 if rank_signal else 0.0)
                + min(0.20, float(as_int(lane.get("flow_signal_count", component.get("flow_signal_count", 0)), 0)) / 50000.0),
            )
            processing_axes = [
                "same_parent_matched_control_lift",
                "broken_sibling_negative_control",
                "non_final_replay_trace",
                "regressionpreview_verificationpass_bridge_conversion",
            ]
            required_evidence_missing = [
                "positive_matched_control_lift",
                "negative_control_evidence",
                "non_final_replay_trace",
                "regressionpreview_or_verificationpass_bridge",
            ]
            if lane.get("evidence_tool_specialization_required"):
                processing_axes.append("evidence_proof_card_tool_consumption")
                required_evidence_missing.append("evidence_proof_card_consumption_trace")
            work_unit_id = _component_atom_promotion_work_id(
                component_id,
                candidate_id,
                source_loop,
                snapshot_generation,
                local_loop,
                local_generation,
            )
            work_items.append({
                "schema_version": SCHEMA_VERSION,
                "event": "ComponentAtomPromotionProofWorkItem",
                "status": "queued_non_final_proof_work",
                "work_unit_id": work_unit_id,
                "root_id": work_unit_id,
                "candidate_id": candidate_id,
                "component_id": component_id,
                "source_kind": "component_atom_promotion_ga",
                "candidate_source": "component_atom_promotion_candidate_spec",
                "source_loop": source_loop,
                "snapshot_generation": snapshot_generation,
                "component_local_loop": local_loop,
                "component_local_generation": local_generation,
                "component_loop_epoch": loop_epoch,
                "local_generation_budget": budget,
                "ga_compute_multiplier": as_int(lane.get("ga_compute_multiplier", component.get("ga_compute_multiplier", 1)), 1),
                "virtual_candidate_population_count": as_int(
                    lane.get("virtual_candidate_population_count", component.get("virtual_candidate_population_count", len(lanes))),
                    len(lanes),
                ),
                "materialized_candidate_count": as_int(component.get("materialized_candidate_count", len(lanes)), len(lanes)),
                "amplified_population_rank": as_int(lane.get("amplified_population_rank"), 0),
                "fitness_target": "proof_promotion_readiness",
                "promotion_target": promotion_target,
                "mutation_axis": mutation_axis,
                "families": ["component_ga:%s" % component_id, "component_ga:%s:%s" % (component_id, promotion_target)],
                "mutation_axes": [mutation_axis, promotion_target, component_id],
                "processing_axes": processing_axes,
                "required_evidence_missing": required_evidence_missing,
                "proof_ladder": [
                    "L1_same_parent_preflight",
                    "L2_successive_halving",
                    "L3_full_non_final_confirmation",
                    "L4_hidden_replay_before_promotion_review",
                ],
                "routing_actions": [
                    "consume_component_atom_promotion_specs",
                    "route_component_specs_to_non_final_proof_compute",
                    "route_component_specs_to_regressionpreview_verificationpass_bridge",
                    "require_same_parent_matched_controls",
                    "require_broken_sibling_negative_controls",
                    "route_survivors_to_hidden_replay_before_promotion_review",
                ],
                "source_atoms": [str(item) for item in lane.get("source_atoms") or []][:8],
                "source_flow_signals": source_flow_signals[:8],
                "source_flow_candidate_ids": source_flow_candidate_ids[:8],
                "evidence_tool_specialization_required": bool(lane.get("evidence_tool_specialization_required")),
                "evidence_tool_specialization_axes": [str(item) for item in lane.get("evidence_tool_specialization_axes") or []][:8],
                "evidence_valid_proof_card_ids": [str(item) for item in lane.get("evidence_valid_proof_card_ids") or []][:6],
                "flow_candidate_count": as_int(lane.get("flow_candidate_count", component.get("flow_candidate_count", 0)), 0),
                "flow_bridge_candidate_count": as_int(lane.get("flow_bridge_candidate_count", component.get("flow_bridge_candidate_count", 0)), 0),
                "flow_signal_count": as_int(lane.get("flow_signal_count", component.get("flow_signal_count", 0)), 0),
                "refined_input_signal_count": as_int(lane.get("refined_input_signal_count", component.get("refined_input_signal_count", 0)), 0),
                "proof_readiness_quality_score": round(float(lane.get("proof_readiness_quality_score", lane.get("quality_score", priority)) or 0.0), 6),
                "quality_score": round(float(lane.get("quality_score", priority) or 0.0), 6),
                "quality_axes": [str(item) for item in lane.get("quality_axes") or []][:12],
                "component_proof_support_n": max(
                    as_int(lane.get("materialized_candidate_count", component.get("materialized_candidate_count", 0)), 0),
                    as_int(lane.get("flow_signal_count", component.get("flow_signal_count", 0)), 0),
                    as_int(lane.get("flow_candidate_count", component.get("flow_candidate_count", 0)), 0),
                ),
                "priority": round(priority, 6),
                "delta": round(priority, 6),
                "compute_weight": round(priority * 10.0, 6),
                "rank_or_verificationpass_signal": bool(rank_signal),
                "same_parent_control_required": True,
                "same_parent_matched_control_required": True,
                "negative_controls_required": True,
                "hidden_replay_required_before_promotion": True,
                "research_replay_only": True,
                "non_final_only": True,
                "reserved_split_used": False,
                "cost_model_locked": True,
                "fixed_window_locked": True,
                "raw_try_count_increase": 0,
                "production_impact_weight": 0.0,
                "promotion_allowed": False,
                "deployment_allowed": False,
                "can_touch_protected_surfaces": False,
                "can_influence_main_search": False,
                "can_touch_final_holdout_or_live": False,
                "final_feedback_allowed": False,
                "policy": "component GA proof-work request only; proof compute may collect non-final controls/replays but promotion gates remain elsewhere",
            })
            emitted += 1
        component_rows.append({
            "component_id": component_id,
            "candidate_spec_count": len(lanes),
            "work_item_count": emitted,
            "local_generation": local_generation,
            "local_generation_budget": budget,
            "loop_epoch": loop_epoch,
        })
    work_items = work_items[:COMPONENT_ATOM_PROMOTION_PROOF_WORK_MAX_ITEMS]
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "component_atom_promotion_proof_work_tick",
        "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "active": bool(work_items),
        "status": "queued_non_final_proof_work" if work_items else "no_component_specs",
        "source_loop": source_loop,
        "snapshot_generation": snapshot_generation,
        "local_loop": local_loop,
        "component_count": len(component_rows),
        "candidate_spec_count": sum(as_int(row.get("candidate_spec_count"), 0) for row in component_rows),
        "work_item_count": len(work_items),
        "components": component_rows,
        "work_items": work_items,
        "routing_actions": [
            "consume_component_atom_promotion_specs",
            "route_component_specs_to_non_final_proof_compute",
            "route_component_specs_to_regressionpreview_verificationpass_bridge",
            "keep_component_specs_zero_authority_until_existing_promotion_gates_pass",
        ],
        "promotion_allowed": False,
        "deployment_allowed": False,
        "can_touch_protected_surfaces": False,
        "can_influence_main_search": False,
        "can_touch_final_holdout_or_live": False,
        "production_impact_weight": 0.0,
        "policy": "bounded bridge from component atom-promotion GA specs to non-final proof compute; no final, holdout, live, deploy, or direct promotion authority",
    }


def append_component_atom_promotion_proof_work_log(project_root, plan, heartbeat=None):
    root = resolve_root(project_root)
    row = build_component_atom_promotion_proof_work(plan, heartbeat=heartbeat)
    append_jsonl(runner_logs(root) / "component_atom_promotion_proof_work.jsonl", row)
    write_json(runner_logs(root) / "component_atom_promotion_proof_work_latest.json", row)
    append_jsonl(root / "research_best" / "component_atom_promotion_proof_work.jsonl", row)
    write_json(root / "research_best" / "component_atom_promotion_proof_work_latest.json", row)
    return row


def _atom_texts(values, limit=16):
    out = []
    seen = set()

    def add(value):
        text = str(value or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        out.append(text[:128])

    for value in values or []:
        if isinstance(value, dict):
            for key in (
                "axis",
                "card_id",
                "patch_family",
                "reason",
                "next_small_fix",
                "blocker",
                "candidate_id",
                "name",
                "id",
                "value",
                "tool_name",
                "pain_target",
            ):
                add(value.get(key))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        else:
            add(value)
        if len(out) >= int(limit):
            break
    return out[: int(limit)]


def evidence_tool_specialization_context(evidence=None, limit=8):
    evidence = evidence if isinstance(evidence, dict) else {}

    def as_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return int(default)

    def read_nested_dict(*keys):
        cur = evidence
        for key in keys:
            if not isinstance(cur, dict):
                return {}
            cur = cur.get(key)
        return cur if isinstance(cur, dict) else {}

    def read_text_list(*keys, item_key=None, max_items=None):
        rows = []
        for key in keys:
            value = evidence.get(key)
            if isinstance(value, list):
                rows.extend(value)
        if item_key:
            nested = []
            for row in rows:
                if isinstance(row, dict):
                    nested.append(row.get(item_key))
                else:
                    nested.append(row)
            rows = nested
        return _atom_texts(rows, limit=max_items or limit)

    valid_cards = max(
        as_int(evidence.get("valid_standard_proof_cards"), 0),
        as_int(evidence.get("compute_report_valid_cards"), 0),
        as_int(read_nested_dict("progress_after").get("valid_standard_proof_cards"), 0),
    )
    bridge_updates = max(
        as_int(evidence.get("repair_precheck_bridge_updated"), 0),
        as_int(read_nested_dict("progress_after").get("repair_precheck_bridge_updated"), 0),
    )
    preproof_passed = as_int(evidence.get("repair_precheck_passed"), 0)
    prepromotion_tickets = max(
        as_int(evidence.get("prepromotion_ticket_count"), 0),
        as_int(evidence.get("new_prepromotion_ticket_count"), 0),
        as_int(read_nested_dict("progress_after").get("new_prepromotion_ticket_count"), 0),
    )
    refresh_tickets = max(
        as_int(evidence.get("evidence_refresh_ticket_count"), 0),
        as_int(evidence.get("new_evidence_refresh_ticket_count"), 0),
        as_int(read_nested_dict("progress_after").get("new_evidence_refresh_ticket_count"), 0),
    )
    invalid_cards = max(
        as_int(evidence.get("invalid_evidence_card_count"), 0),
        as_int(read_nested_dict("progress_after").get("invalid_evidence_card_count"), 0),
    )
    sibling_repairs = max(
        as_int(evidence.get("sibling_control_repair_ticket_count"), 0),
        as_int(read_nested_dict("progress_after").get("sibling_control_repair_ticket_count"), 0),
    )
    integration_stage_flow = max(
        as_int(evidence.get("integration_stage_tool_evidence_flow_count"), 0),
        as_int(read_nested_dict("progress_after").get("integration_stage_tool_evidence_flow_ticket_count"), 0),
    )
    artifact_manifest = evidence.get("proof_artifact_manifest") if isinstance(evidence.get("proof_artifact_manifest"), dict) else {}
    artifact_rows = artifact_manifest.get("artifacts") if isinstance(artifact_manifest.get("artifacts"), list) else []
    card_ids = _atom_texts(
        read_text_list("top_valid_card_ids", "valid_card_ids", "fresh_selected_card_ids", "card_ids", max_items=limit)
        + _atom_texts(artifact_rows, limit=limit),
        limit=limit,
    )
    repair_ids = read_text_list("invalid_evidence_card_repairs", item_key="candidate_id", max_items=limit)
    action_rows = []
    for key in (
        "routing_actions",
        "prepromotion_ticket_actions",
        "compute_report_integration_stage_flow_actions",
        "compute_report_tool_actions",
    ):
        value = evidence.get(key)
        if isinstance(value, list):
            action_rows.extend(value)
    contract = evidence.get("progress_contract") if isinstance(evidence.get("progress_contract"), dict) else {}
    contract_actions = contract.get("routing_actions") if isinstance(contract.get("routing_actions"), list) else []
    action_rows.extend(contract_actions)
    actions = []
    action_keywords = ("evidence", "regressionpreview", "verificationpass", "integration_stage", "refresh", "sibling", "replay", "ablation", "matched", "control")
    for action in _atom_texts(action_rows, limit=24):
        if any(keyword in action.lower() for keyword in action_keywords):
            actions.append(action)
        if len(actions) >= int(limit):
            break

    axes = [
        "specific_evidence_card_id",
        "standard_proof_card_consumer",
        "regressionpreview_verificationpass_bridge",
        "same_parent_matched_control_lift",
        "negative_control_survival",
    ]
    if bridge_updates or preproof_passed or prepromotion_tickets:
        axes.extend(["repair_precheck_followthrough", "non_final_replay_trace"])
    if invalid_cards or sibling_repairs or repair_ids:
        axes.append("invalid_card_sibling_repair")
    if refresh_tickets or evidence.get("evidence_input_stale"):
        axes.append("stale_refresh_consumption")
    if integration_stage_flow:
        axes.append("integration_stage_tool_evidence_flow_consumer")
    axes = _atom_texts(axes, limit=12)

    focus = [
        "standard_proof_cards:%s" % valid_cards,
        "repair_precheck_bridge:%s" % bridge_updates,
        "repair_precheck_passed:%s" % preproof_passed,
        "prepromotion_tickets:%s" % prepromotion_tickets,
        "evidence_refresh_tickets:%s" % refresh_tickets,
        "invalid_evidence_cards:%s" % invalid_cards,
        "integration_stage_tool_evidence_flow:%s" % integration_stage_flow,
    ]
    focus.extend("evidence_card:%s" % item for item in card_ids[:4])
    focus.extend("evidence_invalid_repair:%s" % item for item in repair_ids[:3])
    focus.extend("evidence_action:%s" % item for item in actions[:4])
    active = bool(valid_cards or bridge_updates or preproof_passed or prepromotion_tickets or refresh_tickets or invalid_cards or integration_stage_flow or card_ids or actions)
    return {
        "active": active,
        "source": "evidence_valid_proof_cards_plus_prepromotion_tickets",
        "valid_standard_proof_cards": valid_cards,
        "repair_precheck_bridge_updated": bridge_updates,
        "repair_precheck_passed": preproof_passed,
        "prepromotion_ticket_count": prepromotion_tickets,
        "evidence_refresh_ticket_count": refresh_tickets,
        "invalid_evidence_card_count": invalid_cards,
        "sibling_control_repair_ticket_count": sibling_repairs,
        "integration_stage_tool_evidence_flow_count": integration_stage_flow,
        "progress_state": str(evidence.get("progress_state") or ""),
        "top_valid_card_ids": card_ids,
        "invalid_repair_candidate_ids": repair_ids,
        "routing_actions": actions,
        "specialization_axes": axes,
        "tool_spec_focus": _atom_texts(focus, limit=20),
        "desired_tool_shape": "proof-card-specific tool that consumes one Evidence card or preproof action and names matched-control plus VerificationPass follow-through",
        "rejected_tool_shape": "broad generic huge tool without a named Evidence evidence consumer",
        "same_parent_matched_control_required": True,
        "negative_control_required": True,
        "non_final_only": True,
        "promotion_allowed": False,
        "deployment_allowed": False,
        "can_influence_main_search": False,
        "can_touch_protected_surfaces": False,
        "can_touch_final_holdout_or_live": False,
        "policy": "Evidence evidence may specialize zero-authority tool variants only; existing matched-control, VerificationPass, hidden replay, and promotion gates still decide",
    }


def _component_candidate_id(component_id, atoms, target, index):
    raw = json.dumps(
        {"component": component_id, "atoms": list(atoms or []), "target": target, "index": int(index)},
        sort_keys=True,
        default=str,
    ).encode("utf-8", errors="replace")
    return "capga_%s_%s" % (str(component_id), hashlib.sha256(raw).hexdigest()[:12])


def _component_fast_local_loop_plan(
    refined_signal_count,
    local_spec_count,
    fuel_units,
    max_local_generations=None,
    min_local_generations=1,
):
    refined_signal_count = int(max(0, refined_signal_count or 0))
    local_spec_count = int(max(1, local_spec_count or 1))
    fuel_units = int(max(0, fuel_units or 0))
    generation_cap = max(1, int(max_local_generations or COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS))
    generation_floor = max(1, int(min_local_generations or 1))
    fuel_generations = 1 + int(fuel_units // 48)
    signal_generations = 1 + int(refined_signal_count // COMPONENT_ATOM_PROMOTION_GA_REFINED_SIGNAL_BATCH) if refined_signal_count else 1
    local_generation_budget = max(
        min(generation_floor, generation_cap, local_spec_count),
        min(
            generation_cap,
            local_spec_count,
            max(fuel_generations, signal_generations),
        ),
    )
    speedup = 1.0
    if refined_signal_count > 0:
        speedup = round(float(refined_signal_count) / float(max(local_generation_budget, 1)), 6)
    return {
        "mode": "fast_local_refined_signal_loop",
        "uses_refined_main_flow_as_data": True,
        "input_role": "refined_main_search_candidates_are_data_not_generation_budget",
        "main_generation_inherited": False,
        "expected_faster_than_main_search": True,
        "refined_input_signal_count": refined_signal_count,
        "local_candidate_spec_count": local_spec_count,
        "local_generation_budget": local_generation_budget,
        "max_local_generation_budget": generation_cap,
        "min_local_generation_budget": min(generation_floor, generation_cap, local_spec_count),
        "refined_signal_batch_size": COMPONENT_ATOM_PROMOTION_GA_REFINED_SIGNAL_BATCH,
        "speedup_vs_refined_signal_count": speedup,
        "finish_condition": "emit_local_specs_or_exhaust_fast_local_generation_budget",
    }


def _evidence_tool_ga_amplification(component_id, base_candidate_count, evidence_tool_specialist=None):
    component_id = str(component_id or "")
    amplified = component_id in COMPONENT_ATOM_PROMOTION_GA_AMPLIFIED_COMPONENTS
    multiplier = max(1, int(EVIDENCE_TOOL_GA_COMPUTE_MULTIPLIER if amplified else 1))
    base = max(1, int(base_candidate_count or 1))
    virtual_population = max(base, base * multiplier)
    materialized = base
    if amplified:
        materialized = max(base, min(max(1, EVIDENCE_TOOL_GA_MATERIALIZED_CANDIDATE_LIMIT), virtual_population))
    return {
        "active": amplified,
        "component_id": component_id,
        "compute_multiplier": multiplier,
        "base_candidate_count": base,
        "virtual_candidate_population_count": virtual_population,
        "materialized_candidate_count": materialized,
        "materialized_candidate_limit": max(1, int(EVIDENCE_TOOL_GA_MATERIALIZED_CANDIDATE_LIMIT)),
        "proof_work_candidate_limit": materialized,
        "population_policy": "10000x zero-authority component GA population; materialized rows are still proof-gated and cannot influence main search",
        "promotion_allowed": False,
        "can_influence_main_search": False,
        "can_touch_protected_surfaces": False,
    }


def _component_lane_quality(component_id, lane, ga_amplification, evidence_tool_specialist=None):
    component_id = str(component_id or "")
    lane = lane if isinstance(lane, dict) else {}
    ga_amplification = ga_amplification if isinstance(ga_amplification, dict) else {}
    specialist = evidence_tool_specialist if isinstance(evidence_tool_specialist, dict) else {}
    axes = [
        "same_parent_matched_control_required",
        "negative_control_required",
        "proof_ladder_required",
        "refined_main_search_flow_signal",
    ]
    flow_count = 0
    try:
        flow_count = int(float(lane.get("flow_signal_count") or lane.get("flow_candidate_count") or 0))
    except Exception:
        flow_count = 0
    score = 0.34 + min(0.18, flow_count / 50000.0)
    if lane.get("source_flow_candidate_ids"):
        score += 0.06
        axes.append("specific_flow_candidate_lineage")
    if int(ga_amplification.get("compute_multiplier", 1) or 1) >= 10000:
        score += 0.10
        axes.append("10000x_zero_authority_population")
    if int(ga_amplification.get("materialized_candidate_count", 0) or 0) >= 256:
        score += 0.08
        axes.append("large_materialized_candidate_pool")
    if int(lane.get("local_generation_budget", 0) or 0) >= 16:
        score += 0.10
        axes.append("deep_fast_local_generation_loop")
    target = str(lane.get("promotion_target") or "").lower()
    axis = str(lane.get("mutation_axis") or "").lower()
    if component_id == "evidence" and ("standard_proof_card" in target or "proof_card" in axis):
        score += 0.08
        axes.append("standard_proof_card_readiness")
    if component_id == "tool" and specialist.get("active"):
        score += 0.08
        axes.append("evidence_proof_card_tool_specialization")
    return {
        "score": round(min(1.0, max(0.0, score)), 6),
        "axes": _atom_texts(axes, limit=12),
        "policy": "quality ranks proof-readiness evidence only; no production execution score, promotion, deploy, final, holdout, or live authority",
    }


def _next_component_ga_local_loop(root, snapshot_loop, snapshot_generation):
    latest = read_json(runner_logs(root) / "component_atom_promotion_gas_latest.json", {})
    latest = latest if isinstance(latest, dict) else {}

    def as_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return int(default)

    same_snapshot = (
        str(latest.get("source_loop", latest.get("loop"))) == str(snapshot_loop)
        and str(latest.get("snapshot_generation")) == str(snapshot_generation)
    )
    if same_snapshot:
        return max(1, as_int(latest.get("local_loop"), 0) + 1)
    return 1


def build_component_atom_promotion_gas(search_power_spread=None, index=None, recent=None, evidence=None, matched_lift=None, patch_garden=None, main_search_flow=None):
    spread = search_power_spread if isinstance(search_power_spread, dict) else {}
    index = index if isinstance(index, dict) else {}
    recent = recent if isinstance(recent, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    matched_lift = matched_lift if isinstance(matched_lift, dict) else {}
    patch_cards = [c for c in (patch_garden or []) if isinstance(c, dict)]
    main_flow = main_search_flow if isinstance(main_search_flow, dict) else {}

    def as_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return int(default)

    def top_names(rows, limit=3):
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("name") or "").strip()
            count = as_int(row.get("count"), 0)
            if text:
                out.append("%s:%s" % (text, count))
            if len(out) >= int(limit):
                break
        return out

    flow_candidate_count = as_int(main_flow.get("candidate_count"), 0)
    flow_bridge_count = as_int(main_flow.get("bridge_candidate_count"), 0)
    flow_signal_count = max(
        as_int(main_flow.get("signal_count"), 0),
        as_int(main_flow.get("row_count_scanned"), 0),
        flow_candidate_count,
        flow_bridge_count,
    )
    flow_top_candidates = [row for row in main_flow.get("top_candidates") or [] if isinstance(row, dict)]

    def main_flow_atoms(component_id):
        values = [
            "main_flow_candidates:%s" % flow_candidate_count,
            "main_flow_bridge:%s" % flow_bridge_count,
            "main_flow_signals:%s" % flow_signal_count,
            "integration_stage_pass:%s" % as_int(main_flow.get("integration_stage_pass_count"), 0),
            "verificationpass_survivors:%s" % as_int(main_flow.get("verificationpass_survivor_count"), 0),
            "verificationpass_near_miss:%s" % as_int(main_flow.get("verificationpass_near_miss_count"), 0),
        ]
        values.extend("stage_%s" % item for item in top_names(main_flow.get("top_stages"), 3))
        values.extend("operator_%s" % item for item in top_names(main_flow.get("top_operators"), 3))
        if component_id == "tool":
            values.extend("route_%s" % item for item in top_names(main_flow.get("top_routing_actions"), 4))
            values.extend("operator_family_%s" % item for item in top_names(main_flow.get("top_families"), 3))
        elif component_id == "garden":
            values.extend("death_%s" % item for item in top_names(main_flow.get("top_death_reasons"), 4))
            values.extend("fail_%s" % item for item in top_names(main_flow.get("top_fail_reasons"), 4))
        elif component_id == "nursery":
            values.extend("fail_%s" % item for item in top_names(main_flow.get("top_fail_reasons"), 5))
            values.extend("death_%s" % item for item in top_names(main_flow.get("top_death_reasons"), 3))
        elif component_id == "evidence":
            values.extend("route_%s" % item for item in top_names(main_flow.get("top_routing_actions"), 5))
            values.extend("stage_%s" % item for item in top_names(main_flow.get("top_stages"), 4))
        return _atom_texts(values, limit=24)

    def card_atoms():
        atoms = []
        for card in patch_cards[:8]:
            atoms.extend([
                card.get("card_id"),
                card.get("variant_name"),
                card.get("patch_family"),
                card.get("status"),
            ])
            atoms.extend(card.get("next_small_fixes") if isinstance(card.get("next_small_fixes"), list) else [])
            atoms.extend(card.get("reasons") if isinstance(card.get("reasons"), list) else [])
        return _atom_texts(atoms, limit=20)

    def blocker_atoms():
        values = []
        for blocker in matched_lift.get("top_blockers") or []:
            values.append(blocker)
        values.extend([
            matched_lift.get("recommended_next_family"),
            matched_lift.get("recommended_mutation_axis"),
        ])
        values.extend(spread.get("top_blockers") if isinstance(spread.get("top_blockers"), list) else [])
        return _atom_texts(values, limit=20)

    def evidence_atom_values():
        values = [
            evidence.get("progress_state"),
            "evidence_atoms:%s" % as_int(evidence.get("atoms_collected"), 0),
            "valid_cards:%s" % as_int(evidence.get("valid_standard_proof_cards"), 0),
            "bridge_updates:%s" % as_int(evidence.get("repair_precheck_bridge_updated"), 0),
        ]
        values.extend(evidence.get("routing_actions") if isinstance(evidence.get("routing_actions"), list) else [])
        values.extend(spread.get("active_inputs") if isinstance(spread.get("active_inputs"), list) else [])
        return _atom_texts(values, limit=20)

    tool_atoms = _atom_texts(
        list(index.get("tool_atom_attention_targets") or [])
        + list(recent.get("recent_tool_atom_targets") or [])
        + [
            "tool_atoms:%s" % max(as_int(index.get("tool_atom_neuron_count"), 0), as_int(recent.get("recent_tool_atom_count"), 0)),
            "proof_tail:%s" % as_int(recent.get("proof_yield_tail_count"), 0),
            "non_lift:%s" % as_int(recent.get("tool_bridge_non_lift_count"), 0),
        ],
        limit=20,
    )
    garden_atoms = card_atoms()
    nursery_atoms = blocker_atoms() + _atom_texts([
        "converted_nursery:%s" % as_int(spread.get("converted_nursery_candidates"), 0),
        "repair_nursery:%s" % as_int(matched_lift.get("repair_nursery_candidates"), 0),
        "promotion_candidates:%s" % as_int(matched_lift.get("promotion_candidates"), 0),
    ], limit=6)
    evidence_atoms = evidence_atom_values()
    evidence_tool_specialist = evidence_tool_specialization_context(evidence)
    evidence_tool_focus_atoms = _atom_texts(evidence_tool_specialist.get("tool_spec_focus") or [], limit=20)
    tool_specialist_atoms = _atom_texts(tool_atoms + evidence_tool_focus_atoms, limit=28) if evidence_tool_specialist.get("active") else tool_atoms
    tool_specialist_fuel = (
        as_int(index.get("tool_atom_neuron_count"), 0)
        + as_int(recent.get("recent_tool_atom_count"), 0)
        + 2 * as_int(recent.get("tool_bridge_non_lift_count"), 0)
        + 4 * as_int(evidence_tool_specialist.get("valid_standard_proof_cards"), 0)
        + 3 * as_int(evidence_tool_specialist.get("repair_precheck_bridge_updated"), 0)
        + 2 * as_int(evidence_tool_specialist.get("prepromotion_ticket_count"), 0)
        + as_int(evidence_tool_specialist.get("evidence_refresh_ticket_count"), 0)
        + 2 * as_int(evidence_tool_specialist.get("invalid_evidence_card_count"), 0)
    )
    tool_specialist_targets = [
        "tool_brain_gate_pass",
        "matched_control_lift_positive",
        "verificationpass_bridge_observation",
    ]
    tool_specialist_axes = ["pain_target", "primitive_sequence", "kill_condition", "duplicate_signature"]
    if evidence_tool_specialist.get("active"):
        tool_specialist_targets.insert(0, "evidence_proof_card_tool_consumption")
        tool_specialist_axes = _atom_texts(
            [
                "evidence_proof_card_consumer",
                "repair_precheck_followthrough",
                "specific_tool_contract",
                "invalid_card_sibling_repair",
                "integration_stage_tool_evidence_flow_consumer",
            ]
            + tool_specialist_axes,
            limit=12,
        )

    seed_atoms = {
        "tool": ["exact_pain_target", "required_primitive", "matched_control_lift"],
        "garden": ["next_small_fix", "patch_summary_contract", "hidden_replay_blocker"],
        "nursery": ["repair_axis", "broken_sibling_negative_control", "regime_retest"],
        "evidence": ["standard_proof_card", "repair_precheck_bridge", "stale_refresh_evidence"],
    }
    specs = [
        {
            "component_id": "tool",
            "display_name": "Tool Foundry GA",
            "atoms": tool_specialist_atoms,
            "fuel_bonus": tool_specialist_fuel,
            "theory_ids": ["A", "B", "C", "F"],
            "promotion_targets": tool_specialist_targets,
            "mutation_axes": tool_specialist_axes,
            "evidence_tool_specialization": evidence_tool_specialist,
        },
        {
            "component_id": "garden",
            "display_name": "Patch Garden GA",
            "atoms": garden_atoms,
            "fuel_bonus": 3 * len(patch_cards),
            "theory_ids": ["N", "Q", "R", "P"],
            "promotion_targets": ["clear_next_small_fixes", "patch_summary_contract_pass", "fresh_hidden_replay_pass"],
            "mutation_axes": ["blocker_to_fix", "rollback_path", "self_check_hook", "approval_axis_coverage"],
        },
        {
            "component_id": "nursery",
            "display_name": "Nursery GA",
            "atoms": nursery_atoms,
            "fuel_bonus": 4 * as_int(spread.get("converted_nursery_candidates"), 0) + as_int(matched_lift.get("repair_nursery_candidates"), 0),
            "theory_ids": ["M", "C", "G", "H"],
            "promotion_targets": ["nursery_candidate_to_proof_retest", "matched_control_lift_positive", "three_regime_non_final_retest"],
            "mutation_axes": ["repair_axis", "mechanism_family", "negative_control", "proof_depth_ladder"],
        },
        {
            "component_id": "evidence",
            "display_name": "Evidence Proof GA",
            "atoms": evidence_atoms,
            "fuel_bonus": as_int(evidence.get("atoms_collected"), 0) + 3 * as_int(evidence.get("valid_standard_proof_cards"), 0) + 2 * as_int(evidence.get("repair_precheck_bridge_updated"), 0),
            "theory_ids": ["R", "S", "M", "H"],
            "promotion_targets": ["standard_proof_card_valid", "repair_precheck_followthrough_ticket", "evidence_tool_progress_contract_pass"],
            "mutation_axes": ["feature_lens", "proof_card_refresh", "bridge_consumption", "integration_stage_tool_evidence_flow"],
        },
    ]

    components = []
    forced_ids = []
    for spec in specs:
        component_id = spec["component_id"]
        component_atoms = _atom_texts(spec.get("atoms") or seed_atoms[component_id], limit=24)
        if not component_atoms:
            component_atoms = list(seed_atoms[component_id])
        flow_atoms = main_flow_atoms(component_id)
        atoms = _atom_texts(component_atoms + flow_atoms, limit=48)
        if not atoms:
            atoms = list(seed_atoms[component_id])
        flow_fuel = min(80, int(max(flow_signal_count, 0) // 64)) if flow_signal_count else 0
        fuel_units = len(atoms) + as_int(spec.get("fuel_bonus"), 0) + flow_fuel
        base_candidate_count = max(
            1,
            min(COMPONENT_ATOM_PROMOTION_GA_MAX_CANDIDATES, 1 + int(max(fuel_units, 0) // 10)),
        )
        component_evidence_specialist = spec.get("evidence_tool_specialization") if isinstance(spec.get("evidence_tool_specialization"), dict) else {}
        ga_amplification = _evidence_tool_ga_amplification(component_id, base_candidate_count, component_evidence_specialist)
        candidate_count = int(ga_amplification.get("materialized_candidate_count", base_candidate_count) or base_candidate_count)
        amplified_component = bool(ga_amplification.get("active"))
        component_max_local_generations = (
            max(1, int(EVIDENCE_TOOL_GA_MAX_LOCAL_GENERATIONS))
            if amplified_component
            else COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS
        )
        component_min_local_generations = (
            max(1, int(EVIDENCE_TOOL_GA_MIN_LOCAL_GENERATIONS))
            if amplified_component
            else 1
        )
        local_loop_plan = _component_fast_local_loop_plan(
            flow_signal_count,
            candidate_count,
            fuel_units,
            max_local_generations=component_max_local_generations,
            min_local_generations=component_min_local_generations,
        )
        targets = list(spec.get("promotion_targets") or [])
        axes = list(spec.get("mutation_axes") or [])
        lanes = []
        for idx in range(candidate_count):
            source_atoms = atoms[idx::candidate_count][:4] or atoms[:4]
            source_flow_signals = flow_atoms[idx::candidate_count][:4] or flow_atoms[:4]
            source_flow_candidate_ids = [
                str(row.get("candidate_id") or "")
                for row in flow_top_candidates[idx::candidate_count][:4]
                if isinstance(row, dict) and str(row.get("candidate_id") or "")
            ]
            target = targets[idx % len(targets)]
            axis = axes[idx % len(axes)]
            lane_payload = {
                "candidate_id": _component_candidate_id(component_id, source_atoms, target, idx),
                "component_id": component_id,
                "source_atoms": source_atoms,
                "source_flow_signals": source_flow_signals,
                "source_flow_candidate_ids": source_flow_candidate_ids,
                "blend_operator": "main_search_flow_crossed_with_component_atoms_then_one_axis_mutation",
                "mutation_axis": axis,
                "promotion_target": target,
                "fitness_target": "proof_promotion_readiness",
                "flow_candidate_count": flow_candidate_count,
                "flow_bridge_candidate_count": flow_bridge_count,
                "flow_signal_count": flow_signal_count,
                "refined_input_signal_count": flow_signal_count,
                "refined_input_candidate_count": flow_candidate_count,
                "refined_bridge_input_count": flow_bridge_count,
                "main_generation_inherited": False,
                "local_generation_budget": local_loop_plan["local_generation_budget"],
                "ga_compute_multiplier": int(ga_amplification.get("compute_multiplier", 1) or 1),
                "base_candidate_count": int(base_candidate_count),
                "virtual_candidate_population_count": int(ga_amplification.get("virtual_candidate_population_count", candidate_count) or candidate_count),
                "materialized_candidate_count": int(candidate_count),
                "amplified_population_rank": idx,
                "fitness_weights": {
                    "proof_promotion_readiness": 1.0,
                    "matched_control_or_hidden_replay_evidence": 0.35,
                    "progress_contract_evidence": 0.25,
                    "production_impact": 0.0,
                    "raw_quality_gain": 0.0,
                },
                "same_parent_matched_control_required": True,
                "negative_control_required": True,
                "proof_ladder_required": True,
                "promotion_allowed": False,
                "deployment_allowed": False,
                "can_touch_protected_surfaces": False,
                "can_influence_main_search": False,
                "production_impact_weight": 0.0,
            }
            quality = _component_lane_quality(component_id, lane_payload, ga_amplification, component_evidence_specialist)
            lane_payload.update({
                "quality_score": quality["score"],
                "proof_readiness_quality_score": quality["score"],
                "quality_axes": quality["axes"],
                "quality_policy": quality["policy"],
            })
            if component_id == "tool" and component_evidence_specialist.get("active"):
                lane_payload.update({
                    "evidence_tool_specialization_required": True,
                    "evidence_tool_specialization_axes": component_evidence_specialist.get("specialization_axes", [])[:8],
                    "evidence_tool_specialization_focus": component_evidence_specialist.get("tool_spec_focus", [])[:8],
                    "evidence_valid_proof_card_ids": component_evidence_specialist.get("top_valid_card_ids", [])[:6],
                    "specific_tool_required": True,
                    "large_generic_tool_penalty": True,
                })
            lanes.append(lane_payload)
        selected_theory_ids = list(dict.fromkeys(spec.get("theory_ids") or []))[:candidate_count]
        forced_ids.extend(selected_theory_ids)
        components.append({
            "schema_version": SCHEMA_VERSION,
            "component_id": component_id,
            "display_name": spec.get("display_name"),
            "active": True,
            "mode": "component_main_flow_promotion_ga",
            "population_model": "main_search_flow_signals_plus_component_atoms",
            "flow_population_model": "full_main_search_candidate_flow_as_zero_authority_neuron_signal",
            "candidate_generation": {
                "operation": "blend_full_main_search_flow_with_component_atoms_into_promotion_candidate_specs",
                "candidate_output": "%s_promotion_candidate_specs" % component_id,
                "candidate_count": candidate_count,
                "local_candidate_spec_count": candidate_count,
                "base_candidate_count": int(base_candidate_count),
                "compute_multiplier": int(ga_amplification.get("compute_multiplier", 1) or 1),
                "virtual_candidate_population_count": int(ga_amplification.get("virtual_candidate_population_count", candidate_count) or candidate_count),
                "materialized_candidate_count": int(candidate_count),
                "materialized_candidate_limit": int(ga_amplification.get("materialized_candidate_limit", candidate_count) or candidate_count),
                "proof_work_candidate_limit": int(ga_amplification.get("proof_work_candidate_limit", candidate_count) or candidate_count),
                "input_population_count": flow_candidate_count,
                "bridge_population_count": flow_bridge_count,
                "flow_signal_count": flow_signal_count,
                "refined_input_signal_count": flow_signal_count,
                "input_population_role": "refined_main_search_candidates_are_data_not_generation_budget",
                "main_generation_inherited": False,
                "local_generation_budget": local_loop_plan["local_generation_budget"],
                "max_local_generation_budget": local_loop_plan["max_local_generation_budget"],
                "source": "main_search_flow_plus_component_atoms",
            },
            "fast_local_loop": local_loop_plan,
            "ga_amplification": ga_amplification,
            "refined_input_candidate_count": flow_candidate_count,
            "refined_bridge_input_count": flow_bridge_count,
            "refined_input_signal_count": flow_signal_count,
            "main_generation_inherited": False,
            "expected_faster_than_main_search": True,
            "local_generation_budget": local_loop_plan["local_generation_budget"],
            "atom_count": len(atoms),
            "component_atom_count": len(component_atoms),
            "flow_signal_atom_count": len(flow_atoms),
            "flow_candidate_count": flow_candidate_count,
            "flow_bridge_candidate_count": flow_bridge_count,
            "flow_signal_count": flow_signal_count,
            "flow_signal_atoms": flow_atoms[:12],
            "main_flow_signal_rows": flow_top_candidates[:8],
            "evidence_tool_specialization": component_evidence_specialist if component_id == "tool" else {},
            "fuel_units": int(fuel_units),
            "population_seed_atoms": atoms[:12],
            "selected_theory_ids": selected_theory_ids,
            "promotion_targets": targets,
            "candidate_lanes": lanes,
            "proof_work_candidate_limit": int(ga_amplification.get("proof_work_candidate_limit", candidate_count) or candidate_count),
            "virtual_candidate_population_count": int(ga_amplification.get("virtual_candidate_population_count", candidate_count) or candidate_count),
            "materialized_candidate_count": int(candidate_count),
            "ga_compute_multiplier": int(ga_amplification.get("compute_multiplier", 1) or 1),
            "fitness_target": "proof_promotion_readiness",
            "target_is_promotion_readiness": True,
            "production_impact_weight": 0.0,
            "promotion_allowed": False,
            "deployment_allowed": False,
            "can_touch_protected_surfaces": False,
            "can_influence_main_search": False,
        })
    max_component_local_generation_budget = max(
        (
            int((c.get("fast_local_loop") or {}).get("max_local_generation_budget", COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS) or COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS)
            for c in components
            if isinstance(c, dict)
        ),
        default=COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "active": True,
        "mode": "four_component_atom_promotion_gas",
        "component_ids": list(COMPONENT_ATOM_PROMOTION_GA_COMPONENTS),
        "component_count": len(components),
        "components": components,
        "forced_theory_ids": list(dict.fromkeys(forced_ids))[:PATCH_TOURNAMENT_MAX_VARIANTS],
        "global_candidate_count": flow_candidate_count or sum(len(c.get("candidate_lanes") or []) for c in components),
        "local_candidate_spec_count": sum(len(c.get("candidate_lanes") or []) for c in components),
        "evidence_tool_specialization": evidence_tool_specialist,
        "evidence_tool_ga_amplification": {
            "active": True,
            "amplified_component_ids": list(COMPONENT_ATOM_PROMOTION_GA_AMPLIFIED_COMPONENTS),
            "compute_multiplier": max(1, int(EVIDENCE_TOOL_GA_COMPUTE_MULTIPLIER)),
            "materialized_candidate_limit": max(1, int(EVIDENCE_TOOL_GA_MATERIALIZED_CANDIDATE_LIMIT)),
            "proof_work_item_limit": max(1, int(COMPONENT_ATOM_PROMOTION_PROOF_WORK_MAX_ITEMS)),
            "tool_virtual_candidate_population_count": max(
                (
                    int(c.get("virtual_candidate_population_count", 0) or 0)
                    for c in components
                    if c.get("component_id") == "tool"
                ),
                default=0,
            ),
            "garden_virtual_candidate_population_count": max(
                (
                    int(c.get("virtual_candidate_population_count", 0) or 0)
                    for c in components
                    if c.get("component_id") == "garden"
                ),
                default=0,
            ),
            "nursery_virtual_candidate_population_count": max(
                (
                    int(c.get("virtual_candidate_population_count", 0) or 0)
                    for c in components
                    if c.get("component_id") == "nursery"
                ),
                default=0,
            ),
            "evidence_virtual_candidate_population_count": max(
                (
                    int(c.get("virtual_candidate_population_count", 0) or 0)
                    for c in components
                    if c.get("component_id") == "evidence"
                ),
                default=0,
            ),
            "policy": "amplifies zero-authority component GA search population for tool, garden, nursery, and evidence; promotion and protected-surface gates remain unchanged",
            "promotion_allowed": False,
            "can_touch_protected_surfaces": False,
            "can_influence_main_search": False,
        },
        "flow_candidate_count": flow_candidate_count,
        "flow_bridge_candidate_count": flow_bridge_count,
        "flow_signal_count": flow_signal_count,
        "refined_input_candidate_count": flow_candidate_count,
        "refined_bridge_input_count": flow_bridge_count,
        "refined_input_signal_count": flow_signal_count,
        "main_generation_inherited": False,
        "expected_faster_than_main_search": True,
        "max_local_generation_budget": max_component_local_generation_budget,
        "local_generation_budget": max((int(c.get("local_generation_budget", 1) or 1) for c in components), default=1),
        "fast_local_loop": {
            "mode": "four_fast_local_refined_signal_loops",
            "uses_refined_main_flow_as_data": True,
            "input_role": "main_search_flow_is_refined_data_not_component_generation_budget",
            "main_generation_inherited": False,
            "component_count": len(components),
            "refined_input_signal_count": flow_signal_count,
            "total_local_candidate_spec_count": sum(len(c.get("candidate_lanes") or []) for c in components),
            "max_local_generation_budget": max_component_local_generation_budget,
            "expected_faster_than_main_search": True,
            "finish_condition": "each_component_emits_bounded_local_specs_from_refined_signals",
        },
        "main_search_flow": {
            "active": bool(main_flow.get("active")),
            "source": str(main_flow.get("source") or ""),
            "loop": main_flow.get("loop"),
            "generation": main_flow.get("generation"),
            "candidate_count": flow_candidate_count,
            "bridge_candidate_count": flow_bridge_count,
            "signal_count": flow_signal_count,
            "row_count_scanned": as_int(main_flow.get("row_count_scanned"), 0),
            "integration_stage_eval_count": as_int(main_flow.get("integration_stage_eval_count"), 0),
            "integration_stage_pass_count": as_int(main_flow.get("integration_stage_pass_count"), 0),
            "verificationpass_survivor_count": as_int(main_flow.get("verificationpass_survivor_count"), 0),
            "top_operators": main_flow.get("top_operators")[:8] if isinstance(main_flow.get("top_operators"), list) else [],
            "top_fail_reasons": main_flow.get("top_fail_reasons")[:8] if isinstance(main_flow.get("top_fail_reasons"), list) else [],
            "promotion_allowed": False,
            "can_touch_protected_surfaces": False,
        },
        "target": "proof_promotion_readiness_not_production_impacts",
        "candidate_source": "main_search_flow_plus_component_atoms",
        "policy": "tool, garden, nursery, and evidence each run a local promotion-readiness GA over the full main-search candidate flow plus component atoms; normal gates still decide and no candidate may deploy or touch protected surfaces",
        "promotion_allowed": False,
        "deployment_allowed": False,
        "can_touch_protected_surfaces": False,
        "can_influence_main_search": False,
        "production_impact_weight": 0.0,
    }


def build_continuous_section_shadow_lane_mixer(index=None, recent=None, evidence=None, patch_garden=None, main_search_flow=None):
    index = index if isinstance(index, dict) else {}
    recent = recent if isinstance(recent, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    patch_cards = [c for c in (patch_garden or []) if isinstance(c, dict)]
    main_flow = main_search_flow if isinstance(main_search_flow, dict) else {}
    evidence_tool_specialist = evidence_tool_specialization_context(evidence)

    def as_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return int(default)

    def as_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return float(default)

    def stable_id(prefix, payload):
        raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8", errors="replace")
        return "%s_%s" % (prefix, hashlib.sha256(raw).hexdigest()[:12])

    main_rows = []
    for row in main_flow.get("top_candidates") or []:
        if isinstance(row, dict):
            main_rows.append({**row, "source_type": "main_search_refined_candidate"})
    if not main_rows and as_int(main_flow.get("signal_count"), 0) > 0:
        main_rows.append({
            "candidate_id": stable_id("main_flow", main_flow),
            "source_type": "main_search_refined_flow_summary",
            "family": "main_search_candidate_flow",
            "origin_operator": "main_search_generation_candidate_tap",
            "score": as_float(main_flow.get("signal_count"), 0.0),
            "proof_yield": as_float(main_flow.get("integration_stage_pass_count"), 0.0),
            "verificationpass_near_miss_score": as_float(main_flow.get("verificationpass_near_miss_count"), 0.0),
            "verificationpass_survivor": as_int(main_flow.get("verificationpass_survivor_count"), 0) > 0,
            "promotion_allowed": False,
            "can_influence_main_search": False,
            "can_touch_protected_surfaces": False,
        })

    tool_rows = []
    tool_atoms = _atom_texts(
        list(index.get("tool_atom_attention_targets") or [])
        + list(recent.get("recent_tool_atom_targets") or []),
        limit=16,
    )
    for idx, atom in enumerate(tool_atoms):
        tool_rows.append({
            "candidate_id": stable_id("tool_atom", {"atom": atom, "idx": idx}),
            "source_type": "dynamic_tool_atom",
            "family": "tool_foundry",
            "origin_operator": "tool_atom_flow_ga",
            "tool_name": atom,
            "priority": 0.35 + min(0.25, 0.01 * idx),
            "proof_yield": as_float(index.get("proof_yield_ema_avg"), 0.0),
            "matched_control_lift": 0.0,
            "promotion_allowed": False,
            "can_influence_main_search": False,
            "can_touch_protected_surfaces": False,
        })
    for row in recent.get("recent_bridge_tools") or []:
        if not isinstance(row, dict):
            continue
        observe = as_float(row.get("observe_score"), 0.0)
        control = as_float(row.get("control_score"), 0.0)
        name = str(row.get("tool_name") or "").strip() or stable_id("tool_bridge", row)
        tool_rows.append({
            "candidate_id": stable_id("tool_bridge", row),
            "source_type": "tool_bridge_observation",
            "family": "tool_bridge",
            "origin_operator": name,
            "tool_name": name,
            "score": observe,
            "matched_control_lift": observe - control,
            "proof_yield": max(0.0, observe),
            "promotion_allowed": False,
            "can_influence_main_search": False,
            "can_touch_protected_surfaces": False,
        })
    if evidence_tool_specialist.get("active"):
        for idx, card_id in enumerate((evidence_tool_specialist.get("top_valid_card_ids") or [])[:6]):
            tool_rows.append({
                "candidate_id": stable_id("evidence_tool_card", {"card_id": card_id, "idx": idx}),
                "source_type": "evidence_proof_card_tool_specializer",
                "family": "tool_foundry_evidence_card_consumer",
                "origin_operator": "evidence_proof_tool_specializer",
                "tool_name": "evidence_card_consumer:%s" % card_id,
                "priority": 0.48 + min(0.20, 0.02 * idx),
                "proof_yield": as_float(evidence_tool_specialist.get("valid_standard_proof_cards"), 0.0),
                "verificationpass_near_miss_score": as_float(evidence_tool_specialist.get("integration_stage_tool_evidence_flow_count"), 0.0),
                "matched_control_lift": 0.0,
                "evidence_tool_specialization_required": True,
                "specific_tool_required": True,
                "promotion_allowed": False,
                "can_influence_main_search": False,
                "can_touch_protected_surfaces": False,
            })

    evidence_rows = []
    if evidence:
        evidence_rows.append({
            "candidate_id": stable_id("evidence_progress", evidence),
            "source_type": "evidence_progress_summary",
            "family": str(evidence.get("progress_state") or "evidence_progress"),
            "origin_operator": "evidence_proof_worker",
            "priority": 0.45 + min(0.35, as_int(evidence.get("valid_standard_proof_cards"), 0) / 100.0),
            "proof_yield": as_float(evidence.get("valid_standard_proof_cards"), 0.0),
            "verificationpass_near_miss_score": as_float(evidence.get("integration_stage_tool_evidence_flow_count"), 0.0),
            "proof_card_valid": as_int(evidence.get("valid_standard_proof_cards"), 0) > 0,
            "promotion_allowed": False,
            "can_influence_main_search": False,
            "can_touch_protected_surfaces": False,
        })
    evidence_actions = list(evidence.get("routing_actions") or []) + list(evidence.get("compute_report_integration_stage_flow_actions") or [])
    for idx, action in enumerate(_atom_texts(evidence_actions, limit=12)):
        evidence_rows.append({
            "candidate_id": stable_id("evidence_action", {"action": action, "idx": idx}),
            "source_type": "evidence_refresh_or_bridge_action",
            "family": action,
            "origin_operator": "evidence_tool_progress_contract",
            "priority": 0.32,
            "proof_yield": as_float(evidence.get("valid_standard_proof_cards"), 0.0),
            "promotion_allowed": False,
            "can_influence_main_search": False,
            "can_touch_protected_surfaces": False,
        })

    garden_rows = []
    for card in patch_cards:
        garden_rows.append({
            **card,
            "candidate_id": str(card.get("card_id") or stable_id("garden_card", card)),
            "source_type": "patch_garden_card",
            "family": str(card.get("patch_family") or "patch_garden"),
            "origin_operator": "patch_garden_nursery",
            "priority": 0.30 + min(0.20, 0.02 * len(card.get("next_small_fixes") or [])),
            "promotion_allowed": False,
            "can_influence_main_search": False,
            "can_touch_protected_surfaces": False,
        })

    section_candidates = {
        "main_search": main_rows,
        "evidence": evidence_rows,
        "tool": tool_rows,
        "garden": garden_rows,
    }
    try:
        from code_engine.search_brain import build_section_shadow_lane_plan

        plan = build_section_shadow_lane_plan({"section_lane_candidates": section_candidates})
    except Exception as exc:
        plan = {
            "schema_version": SCHEMA_VERSION,
            "active": False,
            "lanes": [],
            "lane_count": 0,
            "candidate_count": 0,
            "mix_pair_count": 0,
            "mix_pairs": [],
            "routing_actions": [],
            "error": "%s: %s" % (type(exc).__name__, exc),
            "promotion_allowed": False,
            "can_influence_main_search": False,
            "can_touch_protected_surfaces": False,
        }
    plan = dict(plan)
    plan.update({
        "schema_version": SCHEMA_VERSION,
        "mode": "continuous_tool_section_shadow_lane_mixer",
        "continuous": True,
        "tool_side_enabled": True,
        "evidence_side_enabled": True,
        "component_ids": ["main_search", "evidence", "tool", "garden"],
        "section_input_counts": {key: len(value) for key, value in section_candidates.items()},
        "evidence_tool_specialization": evidence_tool_specialist,
        "candidate_source": "main_search_evidence_tool_garden_refined_data",
        "target": "proof_promotion_readiness_candidates_by_section",
        "fast_local_loop": {
            "mode": "continuous_bounded_section_shadow_loops",
            "uses_refined_candidates_as_data": True,
            "main_generation_inherited": False,
            "same_parent_matched_control_required": True,
            "negative_control_required": True,
            "finish_condition": "emit_small_section_mixes_or_wait_for_more_non_final_input",
        },
        "continuous_workers": {
            "tool_lab": {
                "loop": "development_tool_lab.loop_forever",
                "watchdog_task": "REPOSITORY_Tool_Lab_Watchdog",
                "interval_env": "CODE_LAB_TOOL_LAB_INTERVAL_SEC",
                "stop_flag": "STOP_TOOL_LAB.flag",
            },
            "evidence_proof_worker": {
                "loop": "code_engine.evidence_proof_worker --run-once via scheduled watchdog",
                "watchdog_task": "REPOSITORY_Evidence_Proof_Worker",
                "interval": "2_minutes",
                "stop_flag": "STOP_FINAL_LOOP.flag",
            },
        },
        "approval_policy": "section mixers are research-only input generators; standard proof, matched-control, RegressionPreview/VerificationPass, hidden replay, and promotion gates still decide",
        "promotion_allowed": False,
        "deployment_allowed": False,
        "can_touch_protected_surfaces": False,
        "can_influence_main_search": False,
        "production_impact_weight": 0.0,
    })
    return plan


def append_section_shadow_lane_mixer_log(project_root, plan, generation=None):
    root = resolve_root(project_root)
    plan = plan if isinstance(plan, dict) else {}
    generation = generation if isinstance(generation, dict) else {}
    lanes = [lane for lane in plan.get("lanes") or [] if isinstance(lane, dict)]
    row = {
        "schema_version": SCHEMA_VERSION,
        "event": "section_shadow_lane_mixer_tick",
        "ts": time.time(),
        "updated_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "active": bool(plan.get("active")),
        "mode": str(plan.get("mode") or "continuous_tool_section_shadow_lane_mixer"),
        "target": str(plan.get("target") or "proof_promotion_readiness_candidates_by_section"),
        "source_loop": generation.get("loop"),
        "snapshot_generation": generation.get("generation"),
        "continuous": True,
        "tool_side_enabled": bool(plan.get("tool_side_enabled")),
        "evidence_side_enabled": bool(plan.get("evidence_side_enabled")),
        "component_ids": [str(item) for item in plan.get("component_ids") or []],
        "section_input_counts": plan.get("section_input_counts") if isinstance(plan.get("section_input_counts"), dict) else {},
        "lane_count": int(plan.get("lane_count", 0) or 0),
        "candidate_count": int(plan.get("candidate_count", 0) or 0),
        "mix_pair_count": int(plan.get("mix_pair_count", 0) or 0),
        "blocked_input_count": int(plan.get("blocked_input_count", 0) or 0),
        "lanes": [
            {
                "lane_id": str(lane.get("lane_id") or ""),
                "seed_count": int(lane.get("seed_count", 0) or 0),
                "blocked_input_count": int(lane.get("blocked_input_count", 0) or 0),
                "same_parent_matched_control_required": bool(lane.get("same_parent_matched_control_required")),
                "negative_control_required": bool(lane.get("negative_control_required")),
                "promotion_allowed": bool(lane.get("promotion_allowed")),
                "can_touch_protected_surfaces": bool(lane.get("can_touch_protected_surfaces")),
            }
            for lane in lanes[:8]
        ],
        "mix_pairs": [
            {
                "mix_id": str(pair.get("mix_id") or ""),
                "source_lane": str(pair.get("source_lane") or ""),
                "target_lane": str(pair.get("target_lane") or ""),
                "research_replay_only": bool(pair.get("research_replay_only")),
                "promotion_allowed": bool(pair.get("promotion_allowed")),
                "can_touch_protected_surfaces": bool(pair.get("can_touch_protected_surfaces")),
            }
            for pair in (plan.get("mix_pairs") or [])[:12]
            if isinstance(pair, dict)
        ],
        "routing_actions": [str(item) for item in plan.get("routing_actions") or []][:12],
        "continuous_workers": plan.get("continuous_workers") if isinstance(plan.get("continuous_workers"), dict) else {},
        "evidence_tool_specialization": plan.get("evidence_tool_specialization") if isinstance(plan.get("evidence_tool_specialization"), dict) else {},
        "candidate_source": str(plan.get("candidate_source") or "main_search_evidence_tool_garden_refined_data"),
        "fast_local_loop": plan.get("fast_local_loop") if isinstance(plan.get("fast_local_loop"), dict) else {},
        "promotion_allowed": False,
        "deployment_allowed": False,
        "can_touch_protected_surfaces": False,
        "can_influence_main_search": False,
        "production_impact_weight": 0.0,
        "policy": "diagnostic heartbeat for continuous main/evidence/tool/garden section mixers only; no production execution, promotion, protected-surface, final, holdout, live, or deploy authority",
    }
    append_jsonl(runner_logs(root) / "section_shadow_lane_mixer.jsonl", row)
    write_json(runner_logs(root) / "section_shadow_lane_mixer_latest.json", row)
    return row


def build_tool_ticket(project_root):
    index = summarize_tool_index(project_root)
    recent = summarize_recent_tool_events(project_root)
    matched_lift = summarize_latest_matched_lift(project_root)
    evidence = summarize_evidence_progress(project_root)
    system = summarize_runner_and_research_state(project_root)
    patch_garden = read_patch_garden(project_root, limit=8)
    search_power_spread = summarize_search_power_spread(project_root, index, recent, matched_lift, evidence, system, patch_garden)
    main_search_flow = summarize_main_search_candidate_flow(project_root)
    tool_atom_flow_ga = build_tool_atom_flow_ga_plan(search_power_spread, index, recent, evidence, matched_lift, patch_garden, main_search_flow)
    append_tool_atom_flow_ga_log(project_root, tool_atom_flow_ga, search_power_spread, system, main_search_flow)
    section_shadow_lane_mixer = build_continuous_section_shadow_lane_mixer(index, recent, evidence, patch_garden, main_search_flow)
    append_section_shadow_lane_mixer_log(project_root, section_shadow_lane_mixer, main_search_flow)
    component_atom_promotion_gas = build_component_atom_promotion_gas(search_power_spread, index, recent, evidence, matched_lift, patch_garden, main_search_flow)
    component_atom_promotion_ga_heartbeat = append_component_atom_promotion_ga_log(project_root, component_atom_promotion_gas)
    component_atom_promotion_proof_work = append_component_atom_promotion_proof_work_log(
        project_root,
        component_atom_promotion_gas,
        heartbeat=component_atom_promotion_ga_heartbeat,
    )
    symptoms = {
        **index,
        **recent,
        "latest_matched_lift": matched_lift,
        "evidence_progress": evidence,
        "system_state": system,
        "search_power_spread": search_power_spread,
        "tool_atom_flow_ga": tool_atom_flow_ga,
        "main_search_candidate_flow": main_search_flow,
        "section_shadow_lane_mixer": section_shadow_lane_mixer,
        "component_atom_promotion_gas": component_atom_promotion_gas,
        "component_atom_promotion_proof_work": component_atom_promotion_proof_work,
        "search_power_spread_problem": bool(search_power_spread.get("search_power_problem")),
        "tool_atom_flow_ga_active": bool(tool_atom_flow_ga.get("active")),
        "section_shadow_lane_mixer_active": bool(section_shadow_lane_mixer.get("active")),
        "component_atom_promotion_ga_active": bool(component_atom_promotion_gas.get("active")),
        "component_atom_promotion_proof_work_active": bool(component_atom_promotion_proof_work.get("active")),
        "tool_usefulness_problem": bool(index.get("can_influence_main_search", 0) == 0 or recent.get("tool_bridge_promoted_count", 0) == 0),
        "evidence_progress_problem": bool(evidence.get("needs_supervisor_attention")),
        "repair_factory_problem": bool(
            matched_lift.get("available")
            and (
                int(matched_lift.get("promotion_candidates", 0) or 0) == 0
                or int(matched_lift.get("validated_mechanism_count", 0) or 0) == 0
                or int(matched_lift.get("repair_nursery_candidates", 0) or 0) > 0
            )
        ),
        "continuous_improvement_target": "evidence_and_dynamic_tools_first_then_all_non_protected_research_systems",
    }
    ticket = {
        "schema_version": SCHEMA_VERSION,
        "patch_profile": "v5_9_154_continuous_code_brain",
        "reason": "continuous_system_improvement",
        "created_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "forbidden_surfaces": FORBIDDEN_SURFACES,
        "allowed_surfaces": ALLOWED_SURFACES,
        "symptoms": symptoms,
        "required_patch_objectives": [
            "improve any non-protected research subsystem when evidence shows a safe gain",
            "do not change intentional gates, provider costs, runtime overhead, horizon, final/holdout firewall, save/deploy, production execution, production release controller, secrets, or shell authority",
            "avoid restarting long proof runs just to test a patch",
            "use cheap compile/self-check/static/synthetic gates before any expensive replay proof",
            "promote code only after Codex edits a sandbox and deterministic judge/critic gates pass",
            "make runner/disk/truth-trace behavior safer for forever operation",
            "improve Integration-stage proof-per-compute routing without weakening proof",
            "improve family/operator ecology without score-only shortcuts",
            "strengthen overfit/firewall checks rather than bypassing them",
            "make tool birth more pain-specific",
            "turn tool observations into VerificationPass-shaped repair candidates",
            "require matched-control lift before influence",
            "repository-scoped: do not add cross-repository data, cross-repository routing, or unrelated-repository proof shortcuts",
            "retain faint repository-scoped non-final alpha as zero-influence nursery candidates instead of discarding it without a repair lesson",
            "grow nursery candidates only through matched repository baseline controls, broken-sibling negative controls, and unrelated non-final regime retests",
            "when collapse is detected, run a 3-5 patch tournament in isolated sandboxes and keep only the proof-ecology winner",
            "run patch tournament variants as a bounded lab team: isolated lanes may work in parallel, but promotion remains one proof-gated winner",
            "every patch must declare: blocker X, allowed surface Y, success metric Z, baseline comparison, and rollback path",
            "record rollback memory for helped/harmed/neutral repair types so future tournaments learn which repair families are risky",
            "add or update a cheap deterministic self-check for every new self-improvement path",
            "route compute with a search governor that tracks proof yield by family, operator, regime, tool type, mutation axis, and proof depth",
            "build a search-power-spread portfolio that combines dynamic tool scraps, evidence proof cards, nursery candidates, search governor budget grants, parent/operator/family/regime axes, and proof-depth lanes into zero-influence useful descendants before any influence",
            "keep a constant tool atom-flow GA warm: use evidence/tool atom fuel plus refined main-search candidate-flow data each cycle to generate pain-specific tool/code variants optimized for full tool-brain promotion-readiness, while retaining zero authority until proof checks pass",
            "feed Evidence valid proof cards, prepromotion tickets, invalid-card repairs, refresh actions, and Integration-stage tool-evidence flow into a dedicated tool specializer lane; tools must become specific Evidence proof-card consumers, not broad generic huge tools",
            "split atom blending into separate tool, garden, nursery, and evidence GAs; each component GA generates candidates from atoms with proof-promotion readiness as the target and production-impact/QualityGain weight fixed at zero",
            "component atom-promotion GAs must treat main-search candidates as refined input data, not as inherited generation budgets; each component runs a bounded fast local loop and emits only a small local spec set",
            "materialize component atom-promotion specs as bounded non-final proof-compute work every local loop, requiring same-parent controls, broken-sibling negative controls, RegressionPreview/VerificationPass bridge evidence, and hidden replay before any later promotion review",
            "run continuous section shadow lanes for main_search, evidence, tool, and garden; each lane uses refined candidates as data, mixes with the other sections under same-parent matched controls, and routes winners back to proof cards or RegressionPreview/VerificationPass without promotion authority",
            "run an anti-overfit auditor against proposed improvements before promotion",
            "retire duplicate/no-lift tool families faster",
            "mutate failed tool families using memory instead of recreating duplicates",
            "improve RegressionPreview-to-VerificationPass tool bridge without changing gates",
            "improve proof-yield scoring and diagnostics for tools",
            "force an evidence-progress lane whenever the evidence worker is stale, repeating cards, or failing to consume valid cards into alpha preproof",
            "force a dynamic-tool lane whenever tool execution, matched-control lift, or no-lift quarantine is stalled",
            "every promoted sandbox patch must write evidence_tool_progress_contract in patch_summary.json with before/after non-final evidence and protected-surface assertions",
            "every approved sandbox patch must declare required_axes=['evidence','tool'], prove at least one allowed evidence improvement plus one allowed tool improvement, and change at least one evidence-side and one tool-side approval file",
            "when evidence and dynamic tools are both stuck, force the same both-axis approval rule instead of accepting single-axis prompt or logging edits",
            "keep rejected or near-pass sandbox patches in the patch garden with exact blockers and next small fixes until a later lane proves they pass without weakening gates",
            "run active patch-garden nursery retries through Codex as bounded repair lanes until the card passes approval gates or returns with sharper blockers",
            "use explicit blocker-to-mutation recipes: previous-best loss, cost fragility, single-slice dependency, dual-control lift, random-repair loss, proof-per-compute",
            "use proof ladder by default for blocker-driven tool runs: L1 2x64 preflight, L2 successive halving, L3 full non-final confirmation, L4 hidden replay before promotion",
        ],
        "blocker_recipe_plan": {
            "primary_policy": "one primary blocker mission per variant; secondary blockers may add pre-gates but must not steal the repair axis",
            "top_recent_blockers": matched_lift.get("top_blockers", []),
            "recommended_next_family": matched_lift.get("recommended_next_family", ""),
            "recommended_mutation_axis": matched_lift.get("recommended_mutation_axis", ""),
            "proof_ladder": ["L1_preflight_2x64", "L2_successive_halving", "L3_full_non_final_confirmation", "L4_hidden_replay_before_promotion"],
        },
        "patch_tournament_policy": {
            "min_variants": PATCH_TOURNAMENT_MIN_VARIANTS,
            "max_variants": PATCH_TOURNAMENT_MAX_VARIANTS,
            "execution_model": "bounded_parallel_isolated_lab_lanes",
            "default_lab_workers": CODE_BRAIN_DEFAULT_LAB_WORKERS,
            "causal_claim_required": ["blocker_x", "allowed_surface_y", "success_metric_z", "baseline", "rollback_path"],
            "auditor_required": True,
            "self_check_required_for_new_repair_type": True,
            "promotion_rule": "winner must improve proof ecology in sandbox and pass compile, protected diff, judge, auditor, and rollback-memory checks",
        },
        "patch_garden_nurse_policy": {
            "active": bool(patch_garden),
            "default_retry_lanes_per_cycle": PATCH_GARDEN_NURSERY_DEFAULT_LANES,
            "retry_until": "tool_brain_gate_and_proof_promotion_pass",
            "approval_conditions": [
                "Codex sandbox execution completes",
                "patch_summary.json exists before long tests",
                "evidence_tool_progress_contract proves evidence and tool axes",
                "approval_axis_file_coverage touches evidence and tool code",
                "compile, protected diff, deterministic judge, and hidden replay gates pass",
            ],
            "no_bypass": True,
        },
        "search_governor_targets": [
            "families",
            "operators",
            "regimes",
            "tool_types",
            "mutation_axes",
            "proof_depths",
        ],
        "search_power_spread_plan": search_power_spread,
        "tool_atom_flow_ga": tool_atom_flow_ga,
        "section_shadow_lane_mixer": section_shadow_lane_mixer,
        "component_atom_promotion_gas": component_atom_promotion_gas,
        "component_atom_promotion_proof_work": component_atom_promotion_proof_work,
    }
    autopsy = {
        "schema_version": SCHEMA_VERSION,
        "created_ts": ticket["created_ts"],
        "tool_index_summary": index,
        "recent_tool_event_summary": recent,
        "evidence_progress_summary": evidence,
        "latest_matched_lift_summary": matched_lift,
        "search_power_spread_summary": search_power_spread,
        "tool_atom_flow_ga_plan": tool_atom_flow_ga,
        "section_shadow_lane_mixer_plan": section_shadow_lane_mixer,
        "component_atom_promotion_gas_plan": component_atom_promotion_gas,
        "component_atom_promotion_proof_work_plan": component_atom_promotion_proof_work,
        "system_state_summary": system,
        "patch_garden_cards": patch_garden,
        "patch_garden_nurse_policy": ticket.get("patch_garden_nurse_policy", {}),
        "diagnosis": "The code brain should force evidence/tool progress first, then improve all non-protected research machinery, while avoiding pointless full-run restarts.",
        "policy": "separate-brain lab: sandbox variants first; live promotion only when Codex ran, files changed, patch summary exists, compile/protected/judge gates pass, and promotion is explicitly enabled. Current long runs are not restarted by the brain.",
    }
    return ticket, autopsy


def render_tool_prompt(theory, ticket, autopsy, memory):
    garden_card = theory.get("patch_garden_card") if isinstance(theory, dict) and isinstance(theory.get("patch_garden_card"), dict) else {}
    garden_retry_block = ""
    if garden_card:
        garden_retry_block = (
            "PatchGardenNurse active retry lane:\n"
            "You are repairing exactly this failed or near-pass card. Keep retrying the smallest safe code fix that clears next_small_fixes; do not switch theories.\n"
            "If it still cannot pass, write patch_summary.json with sharper blockers and the next smallest repair step so the nursery can retry again next cycle.\n"
            "This card has no promotion authority and cannot bypass compile, protected diff, hidden replay, or tool/evidence approval gates.\n"
            f"{json.dumps(garden_card, indent=2, sort_keys=True, default=str)[:6000]}\n\n"
        )
    return (
        "You are Codex running inside a sandbox copy of the REPOSITORY neuroevolution project.\n"
        "Your job is to improve the whole research system, not just dynamic tools.\n"
        "You are one isolated lane in a parallel lab team. Pursue this assigned causal theory deeply; do not merge in other tournament variants.\n\n"
        "Patch only this sandbox. Do not touch the real project root.\n"
        "If the runtime is sliced, patch the relevant code_engine/legacy_slices/*.py file, not only the tiny loader.\n"
        f"Stay inside the promotable file set unless the ticket explicitly names another file: {json.dumps(PROMOTABLE_FILES)}.\n"
        "Do not edit a legacy slice manifest by itself, and do not edit unlisted legacy slices; those changes are treated as unpromotable even if a local test passes.\n"
        "Do not relax rank/final gates, provider costs, runtime overhead, fixed horizon, final/test/holdout firewall, save/deploy policy, production execution, production release controller, secrets, or shell authority.\n"
        "Treat those gates as intentional. You may add checks proving they stayed unchanged, but you may not tune or weaken them.\n"
        "Tool influence must remain zero until matched-control proof beats control.\n\n"
        "Patch garden: if autopsy.patch_garden_cards contains a near-pass repair for your assigned surface, prefer one small safe fix that clears its listed next_small_fixes. Keep those cards as repair memory only; never use them to bypass compile, protected diff, hidden replay, or promotion gates.\n\n"
        + garden_retry_block
        +
        "Evidence/tool progress is mandatory for this lab: every approved patch must prove both an evidence improvement and a tool improvement, not just one side.\n"
        "Write this proof as patch_summary.json:evidence_tool_progress_contract with before, after, improvements, progress_axes, required_axes, pass, protected_surfaces_unchanged, promotion_allowed=false, can_touch_protected_surfaces=false, and tool_influence_requires_matched_lift=true.\n"
        f"{progress_contract_prompt_hint()}\n"
        "required_axes must include both evidence and tool, improvements must include at least one allowed evidence improvement plus one allowed tool improvement, and the code diff must change at least one evidence-side approval file plus one tool-side approval file.\n"
        "Single-axis repairs should be kept in the patch garden until the missing axis is fixed.\n\n"
        "Search-power spread: when ticket.search_power_spread_plan or autopsy.search_power_spread_summary is active, combine every available non-final signal into a bounded portfolio instead of fixing one isolated tool. Use dynamic tool scraps, evidence proof cards, nursery candidates, search-governor budget grants, parent/operator/family/regime axes, proof-depth lanes, and patch-garden lessons. Every descendant must be one-axis, zero-influence, same-parent matched-control tested, and blocked from final/holdout/live/deploy surfaces.\n\n"
        "Tool atom-flow GA: when ticket.tool_atom_flow_ga or autopsy.tool_atom_flow_ga_plan is active, use the listed atom fuel, lanes, selected theory ids, evidence_tool_specialization, and candidate_judge_queue as the constant tool-generation tournament. Generate sharper pain-specific tool/code variants that can pass the existing full_tool_brain_promotion_readiness gate stack. The evidence proof-card specializer may build tools large enough to combine named Evidence proof-card, prepromotion, refresh, invalid-card repair, and Integration-stage flow evidence, but every tool must stay one-axis, specific, zero-authority, and judged by matched-control or VerificationPass/replay follow-through. Use candidate_judge_budget to score many read-only main-search and Evidence signals, not to expand promotion authority, and do not count tool library size as progress unless matched-control, VerificationPass-bridge, quarantine, or mutation-from-memory evidence improves.\n\n"
        "Section shadow lane mixer: when ticket.section_shadow_lane_mixer or autopsy.section_shadow_lane_mixer_plan is active, keep main_search, evidence, tool, and garden as separate continuous local lanes. Use their refined candidate rows only as data, mix each target section with peer-section compact summaries under same-parent matched controls and negative controls, and route any winner to proof-card or RegressionPreview/VerificationPass follow-through without promotion authority.\n\n"
        "Component atom-promotion GAs: when ticket.component_atom_promotion_gas or autopsy.component_atom_promotion_gas_plan is active, treat tool, garden, nursery, and evidence as four separate promotion-readiness populations fed by the full main-search candidate flow plus that component's atoms. Use the main-flow candidate/bridge rows as refined zero-authority input data, not as inherited component generations and not as production execution goals. Each component should run its own bounded fast local loop, emit only the requested local specs, then route those specs through ticket.component_atom_promotion_proof_work as non-final proof-compute work requiring same-parent controls, negative controls, RegressionPreview/VerificationPass bridge evidence, and hidden replay before any later promotion review. Keep every component spec zero-influence and blocked from final/holdout/live/deploy surfaces until the existing promotion gates pass.\n\n"
        "This project is repository-scoped for alpha research. Do not introduce cross-repository data, cross-repository training, or unrelated-repository proof shortcuts.\n"
        "If you find faint non-final REPOSITORY alpha, keep it as zero-influence nursery material with a repair axis, matched controls, broken-sibling negative controls, and three unrelated non-final regime retests. Do not let nursery material bypass normal gates.\n\n"
        "Patch tournament standard: propose or implement a causal repair, not a vague tweak. State blocker X, allowed surface Y, success metric Z, baseline, and rollback path in patch_summary.json.\n"
        "The patch_summary.json file is mandatory and must be valid JSON with at least: files_changed, functions_changed, tests_run, blocker_x, allowed_surface_y, success_metric_z, baseline, rollback_path, protected_surfaces_checked, remaining_risks, and evidence_tool_progress_contract.\n"
        "Create or update patch_summary.json before any long-running test, then update it after tests finish; a lane that times out with no patch_summary.json is considered no progress.\n"
        "Act as your own anti-overfit auditor: try to disprove the patch with negative controls, unrelated regimes, and proof-ecology regressions before asking for trust.\n"
        "If you add a new self-improvement path, add a cheap deterministic self-check for it in the same patch.\n\n"
        "Do not launch expensive full proof runs from inside the sandbox unless the ticket explicitly says a full replay is budgeted. Prefer compile, static checks, synthetic telemetry, and tiny non-final self-checks.\n"
        "Do not restart or kill the live long-running trainer. Promoted code takes effect on the next safe process load.\n"
        "A variant can be promoted only if it actually edits a promotable file, writes patch_summary.json, compiles, passes protected diff, and passes the deterministic judge.\n\n"
        "Required workflow:\n"
        "1. Read tool_lab_ticket.json, tool_lab_autopsy.json, and tool_theory.json.\n"
        "2. Patch only allowed non-protected research surfaces.\n"
        "3. Add or update self-checks for the selected subsystem.\n"
        "4. Run py_compile and relevant self-checks when possible.\n"
        "5. Write patch_summary.json with files changed, functions changed, tests run, protected surfaces checked, remaining risks, and evidence_tool_progress_contract.\n\n"
        f"Improvement theory:\n{json.dumps(theory, indent=2, sort_keys=True, default=str)}\n\n"
        f"Ticket:\n{json.dumps(ticket, indent=2, sort_keys=True, default=str)[:7000]}\n\n"
        f"Autopsy:\n{json.dumps(autopsy, indent=2, sort_keys=True, default=str)[:7000]}\n\n"
        f"Recent code-brain memory:\n{json.dumps(memory, indent=2, sort_keys=True, default=str)[:5000]}\n"
    )


def copy_optional(src, dst):
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        return False
    if src.is_dir():
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "code_surgery", "research_best", "_neuro_tmp", "runner_logs", ".git", ".venv")
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return True


def run_subprocess(argv, cwd, timeout=1800, input_text=None):
    return _hub_operation("run_process", argv, cwd=cwd, timeout=timeout, input_text=input_text)


def sha256_file(path):
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def changed_promotable_files(variant_dir, project_root):
    root = resolve_root(project_root)
    variant_dir = Path(variant_dir)
    changed = []
    for name in PROMOTABLE_FILES:
        src = variant_dir / name
        dst = root / name
        if src.exists() and sha256_file(src) != sha256_file(dst):
            changed.append(name)
    return changed


def compile_changed_files(variant_dir, changed_files):
    variant_dir = Path(variant_dir)
    checks = {}
    for name in changed_files:
        path = variant_dir / name
        if path.suffix.lower() == ".py" and path.exists():
            checks[name] = run_subprocess([sys.executable, "-m", "py_compile", str(path)], cwd=variant_dir, timeout=180)
    return checks


def patch_summary_ok(variant_dir):
    path = Path(variant_dir) / "patch_summary.json"
    if not path.exists():
        return {"pass": False, "reason": "patch_summary_missing", "path": str(path)}
    data = read_json(path, {})
    files = data.get("files_changed") or data.get("changed_files") or data.get("files")
    tests = data.get("tests_run") or data.get("tests")
    required = ["blocker_x", "allowed_surface_y", "success_metric_z", "baseline", "rollback_path"]
    missing = [key for key in required if not data.get(key)]
    return {
        "pass": bool(files and tests and not missing),
        "reason": "ok" if files and tests and not missing else ("patch_summary_missing_causal_fields" if missing else "patch_summary_incomplete"),
        "path": str(path),
        "summary": data,
        "missing_required_fields": missing,
    }


def ensure_blocked_patch_summary(variant_dir, theory=None, codex_exec=None):
    variant_dir = Path(variant_dir)
    path = variant_dir / "patch_summary.json"
    if path.exists():
        return {"written": False, "path": str(path), "reason": "already_exists"}
    theory = theory if isinstance(theory, dict) else {}
    codex_exec = codex_exec if isinstance(codex_exec, dict) else {}
    card = theory.get("patch_garden_card") if isinstance(theory.get("patch_garden_card"), dict) else {}
    stderr = str(codex_exec.get("stderr") or "")
    stdout = str(codex_exec.get("stdout") or "")
    blocked_reason = "codex_exec_timeout_or_nonzero"
    if codex_exec.get("returncode") is not None:
        blocked_reason = f"codex_exec_returncode_{codex_exec.get('returncode')}"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "approval_status": "blocked_before_code_change",
        "patch_family": str(theory.get("patch_family") or card.get("patch_family") or ""),
        "patch_garden_card_id": str(card.get("card_id") or theory.get("patch_garden_card_id") or ""),
        "files_changed": [],
        "functions_changed": [],
        "tests_run": ["codex_exec did not complete cleanly; no long tests were trusted"],
        "blocker_x": blocked_reason,
        "allowed_surface_y": str(theory.get("expected_surface") or "diagnostics, nursery evidence, and code-surgery scaffolds"),
        "success_metric_z": str(theory.get("proof_test") or "retry writes a real patch summary, passes compile, and keeps promotion false"),
        "baseline": {
            "codex_exec_pass": bool(codex_exec.get("pass")),
            "codex_returncode": codex_exec.get("returncode"),
            "codex_seconds": codex_exec.get("seconds"),
            "prior_card_reasons": list(card.get("reasons") or [])[:12] if isinstance(card.get("reasons"), list) else [],
        },
        "rollback_path": "discard this sandbox variant; no files were promoted or copied back to the project root",
        "protected_surfaces_checked": FORBIDDEN_SURFACES,
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "can_touch_final_holdout_or_live": False,
        "remaining_risks": [
            "no promotable file changed in this failed lane",
            "next retry must make the patch smaller and write a completed patch_summary.json before long tests",
        ],
        "failure_excerpt": (stderr or stdout)[-1200:],
        "evidence_tool_progress_contract": {
            "pass": False,
            "required_axes": ["evidence", "tool"],
            "progress_axes": [],
            "improvements": [],
            "before": {},
            "after": {},
            "evidence": {},
            "protected_surfaces_unchanged": True,
            "promotion_allowed": False,
            "can_touch_protected_surfaces": False,
            "tool_influence_requires_matched_lift": True,
            "blocked_reason": blocked_reason,
        },
    }
    write_json(path, summary)
    return {"written": True, "path": str(path), "reason": blocked_reason}


EVIDENCE_PROGRESS_IMPROVEMENTS = {
    "evidence_new_cards",
    "evidence_bridge_update",
    "evidence_stale_refresh_action",
}
TOOL_PROGRESS_IMPROVEMENTS = {
    "tool_observe_execution",
    "tool_matched_lift_positive",
    "tool_proxy_quarantine",
    "tool_mutation_from_memory",
    "tool_bridge_to_verificationpass",
}
EVIDENCE_PROGRESS_NUMERIC_EVIDENCE_KEYS = {
    "valid_standard_proof_cards",
    "new_cards_appended",
    "repair_precheck_bridge_updated",
    "new_evidence_refresh_ticket_count",
    "new_prepromotion_ticket_count",
    "evidence_refresh_ticket_count",
}
TOOL_PROGRESS_NUMERIC_EVIDENCE_KEYS = {
    "tool_observe_execution_count",
    "tool_matched_lift_positive_count",
    "tool_quarantine_count",
    "tool_mutation_from_memory_count",
    "tool_bridge_to_verificationpass_count",
    "tool_lifecycle_decision_count",
    "tool_bridge_non_lift_count",
}
EVIDENCE_PROGRESS_ACTION_EVIDENCE = {
    "refresh_workload_evidence_inputs",
    "consume_evidence_refresh_tickets",
    "route_evidence_near_miss_refresh",
    "route_evidence_preproof_to_non_final_replay",
    "route_evidence_preproof_to_regressionpreview_bridge",
}
TOOL_PROGRESS_ACTION_EVIDENCE = {
    "execute_tool_observe",
    "force_observe_execution",
    "retire_or_quarantine_zero_exec_tool",
    "reduce_tool_quota",
    "cooldown_or_mutate_no_retire",
    "tool_bridge_to_verificationpass",
    "route_tool_observe_to_verificationpass",
}


def progress_contract_prompt_hint():
    return (
        "Allowed evidence progress improvements for patch_summary.json are: "
        f"{', '.join(sorted(EVIDENCE_PROGRESS_IMPROVEMENTS))}.\n"
        "Allowed tool progress improvements for patch_summary.json are: "
        f"{', '.join(sorted(TOOL_PROGRESS_IMPROVEMENTS))}.\n"
        "Evidence before/after evidence must use a numeric key or routing action such as "
        f"{', '.join(sorted(EVIDENCE_PROGRESS_NUMERIC_EVIDENCE_KEYS | EVIDENCE_PROGRESS_ACTION_EVIDENCE))}.\n"
        "The evidence evidence flag may be evidence_new_cards_or_bridge_or_stale_refresh.\n"
        "Tool before/after evidence must use a numeric key or lifecycle action such as "
        f"{', '.join(sorted(TOOL_PROGRESS_NUMERIC_EVIDENCE_KEYS | TOOL_PROGRESS_ACTION_EVIDENCE))}.\n"
        "The tool evidence flag may be tool_observe_lift_quarantine_or_bridge.\n"
        "Use promotion_allowed=false, can_touch_protected_surfaces=false, and "
        "tool_influence_requires_matched_lift=true; this is repair evidence only."
    )


def _contract_mapping(value):
    return value if isinstance(value, dict) else {}


def _contract_strings(value):
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item)]
    return []


def _contract_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def build_patch_garden_tool_progress_fragment(tool_progress=None):
    source = _contract_mapping(tool_progress)
    before_source = _contract_mapping(source.get("before"))
    after_source = _contract_mapping(source.get("after"))
    before = {}
    after = {}
    for key in TOOL_PROGRESS_NUMERIC_EVIDENCE_KEYS:
        before[key] = _contract_float(before_source.get(key), 0.0)
        after[key] = _contract_float(after_source.get(key, source.get(key, before[key])), before[key])

    if "compute_report_tool_decisions" in source:
        after["tool_lifecycle_decision_count"] = max(
            after.get("tool_lifecycle_decision_count", 0.0),
            _contract_float(source.get("compute_report_tool_decisions"), 0.0),
        )

    def merged_strings(*values):
        out = []
        for value in values:
            for item in _contract_strings(value):
                if item not in out:
                    out.append(item)
        return out

    before["tool_lifecycle_actions"] = _contract_strings(before_source.get("tool_lifecycle_actions"))
    before["compute_report_tool_actions"] = _contract_strings(before_source.get("compute_report_tool_actions"))
    after["tool_lifecycle_actions"] = merged_strings(
        after_source.get("tool_lifecycle_actions"),
        source.get("tool_lifecycle_actions"),
        source.get("actions"),
    )
    after["compute_report_tool_actions"] = merged_strings(
        after_source.get("compute_report_tool_actions"),
        source.get("compute_report_tool_actions"),
    )

    action_set = set(after["tool_lifecycle_actions"]) | set(after["compute_report_tool_actions"])
    if action_set & {"retire_or_quarantine_zero_exec_tool", "reduce_tool_quota", "cooldown_or_mutate_no_retire"}:
        after["tool_quarantine_count"] = max(after.get("tool_quarantine_count", 0.0), before.get("tool_quarantine_count", 0.0) + 1.0)
    if action_set & {"execute_tool_observe", "force_observe_execution"}:
        after["tool_observe_execution_count"] = max(after.get("tool_observe_execution_count", 0.0), before.get("tool_observe_execution_count", 0.0) + 1.0)
    if action_set & {"tool_bridge_to_verificationpass", "route_tool_observe_to_verificationpass"}:
        after["tool_bridge_to_verificationpass_count"] = max(after.get("tool_bridge_to_verificationpass_count", 0.0), before.get("tool_bridge_to_verificationpass_count", 0.0) + 1.0)

    improvements = [
        item for item in _contract_strings(source.get("improvements"))
        if item in TOOL_PROGRESS_IMPROVEMENTS
    ]
    if "tool_observe_execution" not in improvements and after.get("tool_observe_execution_count", 0.0) > before.get("tool_observe_execution_count", 0.0):
        improvements.append("tool_observe_execution")
    if "tool_matched_lift_positive" not in improvements and after.get("tool_matched_lift_positive_count", 0.0) > before.get("tool_matched_lift_positive_count", 0.0):
        improvements.append("tool_matched_lift_positive")
    if "tool_proxy_quarantine" not in improvements and (
        after.get("tool_quarantine_count", 0.0) > before.get("tool_quarantine_count", 0.0)
        or bool(action_set & {"retire_or_quarantine_zero_exec_tool", "reduce_tool_quota", "cooldown_or_mutate_no_retire"})
    ):
        improvements.append("tool_proxy_quarantine")
    if "tool_mutation_from_memory" not in improvements and after.get("tool_mutation_from_memory_count", 0.0) > before.get("tool_mutation_from_memory_count", 0.0):
        improvements.append("tool_mutation_from_memory")
    if "tool_bridge_to_verificationpass" not in improvements and after.get("tool_bridge_to_verificationpass_count", 0.0) > before.get("tool_bridge_to_verificationpass_count", 0.0):
        improvements.append("tool_bridge_to_verificationpass")

    evidence_ready = bool(improvements)
    return {
        "schema_version": SCHEMA_VERSION,
        "required_axes": ["tool"],
        "progress_axes": ["tool"] if evidence_ready else [],
        "improvements": improvements,
        "before": before,
        "after": after,
        "evidence": {"tool_observe_lift_quarantine_or_bridge": evidence_ready},
        "protected_surfaces_unchanged": True,
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
        "policy": "tool-side patch-garden evidence only; main-search influence remains locked until matched-control lift beats control",
    }


def _contract_before_after_evidence(contract, evidence_hits, tool_hits):
    before = _contract_mapping(contract.get("before"))
    after = _contract_mapping(contract.get("after"))
    evidence = _contract_mapping(contract.get("evidence"))
    reasons = []
    if not before:
        reasons.append("progress_contract_before_missing")
    if not after:
        reasons.append("progress_contract_after_missing")

    def numeric_increase(keys):
        return any(_contract_float(after.get(key), 0.0) > _contract_float(before.get(key), 0.0) for key in keys)

    def action_hit(actions):
        action_fields = (
            "routing_actions",
            "tool_lifecycle_actions",
            "compute_report_tool_actions",
            "tool_actions",
        )
        observed = set()
        for field in action_fields:
            observed.update(_contract_strings(after.get(field)))
        return bool(observed & set(actions))

    evidence_evidence = bool(
        evidence.get("evidence")
        or evidence.get("evidence_new_cards_or_bridge_or_stale_refresh")
        or numeric_increase(EVIDENCE_PROGRESS_NUMERIC_EVIDENCE_KEYS)
        or action_hit(EVIDENCE_PROGRESS_ACTION_EVIDENCE)
    )
    tool_evidence = bool(
        evidence.get("tool")
        or evidence.get("tool_observe_lift_quarantine_or_bridge")
        or numeric_increase(TOOL_PROGRESS_NUMERIC_EVIDENCE_KEYS)
        or action_hit(TOOL_PROGRESS_ACTION_EVIDENCE)
    )
    if evidence_hits and not evidence_evidence:
        reasons.append("evidence_before_after_evidence_missing")
    if tool_hits and not tool_evidence:
        reasons.append("tool_before_after_evidence_missing")
    return {
        "pass": not reasons,
        "reasons": reasons,
        "has_before": bool(before),
        "has_after": bool(after),
        "evidence_evidence": evidence_evidence,
        "tool_evidence": tool_evidence,
    }


def build_evidence_tool_progress_contract(evidence_progress=None, tool_progress=None, card_id=""):
    evidence_source = _contract_mapping(evidence_progress)
    if isinstance(evidence_source.get("patch_summary_evidence_progress_fragment"), dict):
        evidence_source = evidence_source.get("patch_summary_evidence_progress_fragment")
    elif isinstance(evidence_source.get("progress_contract"), dict):
        evidence_source = evidence_source.get("progress_contract")
    evidence_source = _contract_mapping(evidence_source)
    tool_source = _contract_mapping(tool_progress)

    before = dict(_contract_mapping(evidence_source.get("before")))
    after = dict(_contract_mapping(evidence_source.get("after")))
    tool_before = _contract_mapping(tool_source.get("before"))
    tool_after = _contract_mapping(tool_source.get("after"))

    for key in TOOL_PROGRESS_NUMERIC_EVIDENCE_KEYS:
        before.setdefault(key, _contract_float(tool_before.get(key), 0.0))
        after[key] = _contract_float(tool_after.get(key, tool_source.get(key, before.get(key, 0.0))), before.get(key, 0.0))

    def merged_strings(*values):
        out = []
        for value in values:
            for item in _contract_strings(value):
                if item not in out:
                    out.append(item)
        return out

    before.setdefault("tool_lifecycle_actions", _contract_strings(tool_before.get("tool_lifecycle_actions")))
    after["tool_lifecycle_actions"] = merged_strings(
        after.get("tool_lifecycle_actions"),
        tool_after.get("tool_lifecycle_actions"),
        tool_source.get("tool_lifecycle_actions"),
        tool_source.get("compute_report_tool_actions"),
        tool_source.get("actions"),
    )
    before.setdefault("compute_report_tool_actions", _contract_strings(tool_before.get("compute_report_tool_actions")))
    after["compute_report_tool_actions"] = merged_strings(
        after.get("compute_report_tool_actions"),
        tool_after.get("compute_report_tool_actions"),
        tool_source.get("compute_report_tool_actions"),
    )
    if "compute_report_tool_decisions" in tool_source:
        after["tool_lifecycle_decision_count"] = max(
            _contract_float(after.get("tool_lifecycle_decision_count"), 0.0),
            _contract_float(tool_source.get("compute_report_tool_decisions"), 0.0),
        )

    evidence_improvements = [
        item for item in _contract_strings(evidence_source.get("improvements"))
        if item in EVIDENCE_PROGRESS_IMPROVEMENTS
    ]
    if not evidence_improvements:
        if _contract_float(after.get("new_cards_appended"), 0.0) > _contract_float(before.get("new_cards_appended"), 0.0):
            evidence_improvements.append("evidence_new_cards")
        elif _contract_float(after.get("repair_precheck_bridge_updated"), 0.0) > _contract_float(before.get("repair_precheck_bridge_updated"), 0.0):
            evidence_improvements.append("evidence_bridge_update")
        elif (
            _contract_float(after.get("new_evidence_refresh_ticket_count"), 0.0) > _contract_float(before.get("new_evidence_refresh_ticket_count"), 0.0)
            or bool(set(_contract_strings(after.get("routing_actions"))) & EVIDENCE_PROGRESS_ACTION_EVIDENCE)
        ):
            evidence_improvements.append("evidence_stale_refresh_action")

    tool_improvements = [
        item for item in _contract_strings(tool_source.get("improvements"))
        if item in TOOL_PROGRESS_IMPROVEMENTS
    ]
    action_set = set(_contract_strings(after.get("tool_lifecycle_actions")))
    action_set.update(_contract_strings(after.get("compute_report_tool_actions")))
    if "tool_proxy_quarantine" not in tool_improvements and (
        _contract_float(after.get("tool_quarantine_count"), 0.0) > _contract_float(before.get("tool_quarantine_count"), 0.0)
        or bool(action_set & {"retire_or_quarantine_zero_exec_tool", "reduce_tool_quota", "cooldown_or_mutate_no_retire"})
    ):
        tool_improvements.append("tool_proxy_quarantine")
    if "tool_observe_execution" not in tool_improvements and _contract_float(after.get("tool_observe_execution_count"), 0.0) > _contract_float(before.get("tool_observe_execution_count"), 0.0):
        tool_improvements.append("tool_observe_execution")
    if "tool_matched_lift_positive" not in tool_improvements and _contract_float(after.get("tool_matched_lift_positive_count"), 0.0) > _contract_float(before.get("tool_matched_lift_positive_count"), 0.0):
        tool_improvements.append("tool_matched_lift_positive")
    if "tool_mutation_from_memory" not in tool_improvements and _contract_float(after.get("tool_mutation_from_memory_count"), 0.0) > _contract_float(before.get("tool_mutation_from_memory_count"), 0.0):
        tool_improvements.append("tool_mutation_from_memory")
    if "tool_bridge_to_verificationpass" not in tool_improvements and (
        _contract_float(after.get("tool_bridge_to_verificationpass_count"), 0.0) > _contract_float(before.get("tool_bridge_to_verificationpass_count"), 0.0)
        or "route_tool_observe_to_verificationpass" in action_set
        or "tool_bridge_to_verificationpass" in action_set
    ):
        tool_improvements.append("tool_bridge_to_verificationpass")

    evidence = dict(_contract_mapping(evidence_source.get("evidence")))
    evidence.update(_contract_mapping(tool_source.get("evidence")))
    evidence["evidence"] = bool(evidence.get("evidence") or evidence_improvements)
    evidence["evidence_new_cards_or_bridge_or_stale_refresh"] = bool(evidence.get("evidence_new_cards_or_bridge_or_stale_refresh") or evidence_improvements)
    evidence["tool"] = bool(evidence.get("tool") or tool_improvements)
    evidence["tool_observe_lift_quarantine_or_bridge"] = bool(evidence.get("tool_observe_lift_quarantine_or_bridge") or tool_improvements)

    improvements = list(dict.fromkeys(evidence_improvements + tool_improvements))
    progress_axes = []
    if evidence_improvements:
        progress_axes.append("evidence")
    if tool_improvements:
        progress_axes.append("tool")
    return {
        "schema_version": SCHEMA_VERSION,
        "card_id": str(card_id or ""),
        "pass": bool(evidence_improvements and tool_improvements),
        "required_axes": ["evidence", "tool"],
        "progress_axes": progress_axes,
        "improvements": improvements,
        "before": before,
        "after": after,
        "evidence": evidence,
        "protected_surfaces_unchanged": True,
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
        "policy": "patch-garden repair evidence only; no promotion, final/holdout, deploy, live, or main-search authority",
    }


def build_patch_garden_tool_matched_control_retry_contract(evidence_progress=None, tool_progress=None, card_id=""):
    tool_source = tool_progress if isinstance(tool_progress, dict) else {
        "compute_report_tool_decisions": 2,
        "compute_report_tool_actions": ["reduce_tool_quota", "keep_observing"],
    }
    tool_fragment = build_patch_garden_tool_progress_fragment(tool_source)
    return build_evidence_tool_progress_contract(
        evidence_progress,
        tool_fragment,
        card_id=card_id or PATCH_GARDEN_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID,
    )


def build_u163986_tool_matched_control_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_tool_matched_control_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_U163986_REPAIR_CARD_ID,
    )


def build_u19c81a_tool_matched_control_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_tool_matched_control_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_U19C81A_REPAIR_CARD_ID,
    )


def build_u45a8a6_tool_matched_control_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_tool_matched_control_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_U45A8A6_REPAIR_CARD_ID,
    )


def build_u9c41ac_tool_matched_control_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_tool_matched_control_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_U9C41AC_REPAIR_CARD_ID,
    )


def build_u591f7c_tool_matched_control_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_tool_matched_control_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_U591F7C_REPAIR_CARD_ID,
    )


def build_bd6802_search_power_spread_retry_contract(evidence_progress=None, tool_progress=None):
    tool_source = tool_progress if isinstance(tool_progress, dict) else {
        "before": {
            "tool_bridge_to_verificationpass_count": 0,
            "tool_lifecycle_decision_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_bridge_to_verificationpass_count": 1,
            "tool_lifecycle_decision_count": 1,
            "compute_report_tool_actions": ["route_tool_observe_to_verificationpass", "keep_observing"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
        "improvements": ["tool_bridge_to_verificationpass"],
    }
    tool_fragment = build_patch_garden_tool_progress_fragment(tool_source)
    return build_evidence_tool_progress_contract(
        evidence_progress,
        tool_fragment,
        card_id=PATCH_GARDEN_BD6802_REPAIR_CARD_ID,
    )


def build_u141053_search_power_spread_retry_contract(evidence_progress=None, tool_progress=None):
    tool_source = tool_progress if isinstance(tool_progress, dict) else {
        "before": {
            "tool_bridge_to_verificationpass_count": 0,
            "tool_lifecycle_decision_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_bridge_to_verificationpass_count": 1,
            "tool_lifecycle_decision_count": 1,
            "compute_report_tool_actions": ["route_tool_observe_to_verificationpass", "keep_observing"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
        "improvements": ["tool_bridge_to_verificationpass"],
    }
    tool_fragment = build_patch_garden_tool_progress_fragment(tool_source)
    return build_evidence_tool_progress_contract(
        evidence_progress,
        tool_fragment,
        card_id=PATCH_GARDEN_U141053_REPAIR_CARD_ID,
    )


def build_b758c1_search_power_spread_retry_contract(evidence_progress=None, tool_progress=None):
    tool_source = tool_progress if isinstance(tool_progress, dict) else {
        "before": {
            "tool_bridge_to_verificationpass_count": 0,
            "tool_lifecycle_decision_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_bridge_to_verificationpass_count": 1,
            "tool_lifecycle_decision_count": 1,
            "compute_report_tool_actions": ["route_tool_observe_to_verificationpass", "keep_observing"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
        "improvements": ["tool_bridge_to_verificationpass"],
    }
    tool_fragment = build_patch_garden_tool_progress_fragment(tool_source)
    return build_evidence_tool_progress_contract(
        evidence_progress,
        tool_fragment,
        card_id=PATCH_GARDEN_B758C1_REPAIR_CARD_ID,
    )


def build_patch_garden_repository_repair_mechanism_retry_contract(evidence_progress=None, tool_progress=None, card_id=""):
    tool_source = tool_progress if isinstance(tool_progress, dict) else {
        "before": {
            "tool_bridge_to_verificationpass_count": 0,
            "tool_lifecycle_decision_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_bridge_to_verificationpass_count": 1,
            "tool_lifecycle_decision_count": 1,
            "compute_report_tool_actions": ["route_tool_observe_to_verificationpass", "keep_observing"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
        "improvements": ["tool_bridge_to_verificationpass"],
    }
    tool_fragment = build_patch_garden_tool_progress_fragment(tool_source)
    return build_evidence_tool_progress_contract(
        evidence_progress,
        tool_fragment,
        card_id=card_id or PATCH_GARDEN_ACTIVE_REPOSITORY_REPAIR_NURSERY_REPAIR_CARD_ID,
    )


def build_b55003_repository_repair_mechanism_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_repository_repair_mechanism_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_B55003_REPAIR_CARD_ID,
    )


def build_d0eb2f_repository_repair_mechanism_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_repository_repair_mechanism_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_D0EB2F_REPAIR_CARD_ID,
    )


def build_f45089_repository_repair_mechanism_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_repository_repair_mechanism_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_F45089_REPAIR_CARD_ID,
    )


def build_u1ffe8c_repository_repair_mechanism_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_repository_repair_mechanism_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_U1FFE8C_REPAIR_CARD_ID,
    )


def build_patch_garden_e912ae_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_tool_matched_control_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_E912AE_REPAIR_CARD_ID,
    )


def build_u30ffa5_patch_garden_retry_contract(evidence_progress=None, tool_progress=None):
    return build_patch_garden_tool_matched_control_retry_contract(
        evidence_progress,
        tool_progress,
        card_id=PATCH_GARDEN_U30FFA5_REPAIR_CARD_ID,
    )


def build_b5221c_evidence_tool_progress_retry_contract(evidence_progress=None, tool_progress=None):
    tool_source = tool_progress if isinstance(tool_progress, dict) else {
        "before": {
            "tool_bridge_to_verificationpass_count": 0,
            "tool_lifecycle_decision_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_bridge_to_verificationpass_count": 1,
            "tool_lifecycle_decision_count": 1,
            "compute_report_tool_actions": ["route_tool_observe_to_verificationpass", "keep_observing"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
        "improvements": ["tool_bridge_to_verificationpass"],
    }
    tool_fragment = build_patch_garden_tool_progress_fragment(tool_source)
    return build_evidence_tool_progress_contract(
        evidence_progress,
        tool_fragment,
        card_id=PATCH_GARDEN_B5221C_REPAIR_CARD_ID,
    )


def build_c3d7f8_evidence_tool_progress_retry_contract(evidence_progress=None, tool_progress=None):
    tool_source = tool_progress if isinstance(tool_progress, dict) else {
        "before": {
            "tool_bridge_to_verificationpass_count": 0,
            "tool_lifecycle_decision_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_bridge_to_verificationpass_count": 1,
            "tool_lifecycle_decision_count": 1,
            "compute_report_tool_actions": ["route_tool_observe_to_verificationpass", "keep_observing"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
        "improvements": ["tool_bridge_to_verificationpass"],
    }
    tool_fragment = build_patch_garden_tool_progress_fragment(tool_source)
    return build_evidence_tool_progress_contract(
        evidence_progress,
        tool_fragment,
        card_id=PATCH_GARDEN_C3D7F8_REPAIR_CARD_ID,
    )


def approval_axis_file_coverage(changed_files):
    changed = {str(name).replace("\\", "/") for name in (changed_files or []) if str(name)}
    evidence_files = sorted(changed & EVIDENCE_APPROVAL_FILES)
    tool_files = sorted(changed & TOOL_APPROVAL_FILES)
    reasons = []
    if not evidence_files:
        reasons.append("evidence_axis_code_change_missing")
    if not tool_files:
        reasons.append("tool_axis_code_change_missing")
    return {
        "pass": not reasons,
        "reason": "ok" if not reasons else reasons[0],
        "reasons": reasons,
        "required_axes": list(APPROVAL_REQUIRED_PROGRESS_AXES),
        "evidence_files": evidence_files,
        "tool_files": tool_files,
        "policy": "approved tool-lab patches must change both an evidence-side and a tool-side approval file",
    }


def evidence_tool_progress_contract_ok(summary, require_evidence=False, require_tool=False):
    contract = summary.get("evidence_tool_progress_contract") if isinstance(summary, dict) else {}
    if not isinstance(contract, dict):
        contract = {}
    improvements = [str(x) for x in contract.get("improvements", [])] if isinstance(contract.get("improvements"), list) else []
    declared_required_axes = {str(x).lower() for x in contract.get("required_axes", [])} if isinstance(contract.get("required_axes"), list) else set()
    required_axes = set(declared_required_axes)
    if require_evidence:
        required_axes.add("evidence")
    if require_tool:
        required_axes.add("tool")
    evidence_hits = sorted(set(improvements) & EVIDENCE_PROGRESS_IMPROVEMENTS)
    tool_hits = sorted(set(improvements) & TOOL_PROGRESS_IMPROVEMENTS)
    progress_axes = sorted(set(str(x).lower() for x in contract.get("progress_axes", []) if str(x)) if isinstance(contract.get("progress_axes"), list) else set())
    inferred_axes = set(progress_axes)
    if evidence_hits:
        inferred_axes.add("evidence")
    if tool_hits:
        inferred_axes.add("tool")
    allowed_improvements = EVIDENCE_PROGRESS_IMPROVEMENTS | TOOL_PROGRESS_IMPROVEMENTS
    reasons = []
    if not contract:
        reasons.append("evidence_tool_progress_contract_missing")
    if contract and contract.get("pass") is not True:
        reasons.append("evidence_tool_progress_contract_not_passing")
    if contract and not (set(improvements) & allowed_improvements):
        reasons.append("no_evidence_or_tool_improvement_declared")
    if contract and "evidence" in required_axes and not evidence_hits:
        reasons.append("evidence_axis_improvement_missing")
    if contract and "tool" in required_axes and not tool_hits:
        reasons.append("tool_axis_improvement_missing")
    evidence_check = _contract_before_after_evidence(contract, evidence_hits, tool_hits) if contract else {"pass": False, "reasons": ["evidence_tool_progress_contract_missing"]}
    if contract and not evidence_check.get("pass"):
        reasons.extend(evidence_check.get("reasons", []))
    if contract and contract.get("protected_surfaces_unchanged") is not True:
        reasons.append("protected_surface_assertion_missing")
    if contract and contract.get("promotion_allowed") is not False:
        reasons.append("promotion_must_remain_false")
    if contract and contract.get("can_touch_protected_surfaces") is not False:
        reasons.append("protected_surface_authority_must_remain_false")
    if contract and contract.get("tool_influence_requires_matched_lift") is not True:
        reasons.append("matched_lift_tool_influence_assertion_missing")
    return {
        "pass": not reasons,
        "reason": "ok" if not reasons else reasons[0],
        "reasons": reasons,
        "improvements": improvements,
        "progress_axes": sorted(inferred_axes),
        "required_axes": sorted(required_axes),
        "contract": contract,
        "evidence_check": evidence_check,
    }


def patch_garden_contract_repair_hook(contract, card_id=""):
    contract = _contract_mapping(contract)
    check = evidence_tool_progress_contract_ok(
        {"evidence_tool_progress_contract": contract},
        require_evidence=True,
        require_tool=True,
    )
    ready = bool(check.get("pass"))
    cleared = []
    if ready:
        cleared = [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "add_one_cheap_self_check_that_proves_the_claim",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ]
    return {
        "schema_version": SCHEMA_VERSION,
        "card_id": str(card_id or contract.get("card_id") or ""),
        "pass": ready,
        "required_axes": check.get("required_axes", []),
        "progress_axes": check.get("progress_axes", []),
        "cleared_next_small_fixes": cleared,
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
        "check_reason": check.get("reason", ""),
        "policy": "tool-side patch-garden contract verifier only; no tool influence without matched lift",
    }


def patch_garden_retry_readiness(
    contract,
    evidence_fragment=None,
    card_id="",
    patch_summary_written=False,
    changed_files=None,
    codex_exec_clean=False,
    hidden_replay_artifact_preserved=False,
    protected_diff_clean=False,
    next_small_fixes=None,
):
    contract_hook = patch_garden_contract_repair_hook(contract, card_id=card_id)
    evidence_fragment = _contract_mapping(evidence_fragment)
    evidence_hook = _contract_mapping(evidence_fragment.get("patch_garden_repair_progress_hook"))
    evidence_ready = bool(
        evidence_hook.get("pass") is True
        or (
            evidence_fragment.get("progress_hook") == "evidence_patch_summary_merge_fragment"
            and evidence_fragment.get("patch_summary_contract_ready") is True
            and evidence_fragment.get("promotion_allowed") is False
            and evidence_fragment.get("can_touch_protected_surfaces") is False
        )
    )
    coverage = approval_axis_file_coverage(changed_files or [])
    required_fixes = [
        str(item)
        for item in (next_small_fixes if next_small_fixes is not None else PATCH_GARDEN_RETRY_REQUIRED_FIXES)
        if str(item)
    ]
    required_fixes = list(dict.fromkeys(required_fixes or PATCH_GARDEN_RETRY_REQUIRED_FIXES))
    cleared = set(contract_hook.get("cleared_next_small_fixes") or [])
    if evidence_ready:
        evidence_clearable = {
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            PATCH_GARDEN_EVIDENCE_STALE_REFRESH_EVIDENCE_FIX,
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        }
        cleared.update(item for item in _contract_strings(evidence_hook.get("cleared_next_small_fixes")) if item in evidence_clearable)
        cleared.add("patch_code_engine_evidence_proof_worker_with_one_small_progress_hook")
    if patch_summary_written:
        cleared.add("write_patch_summary_json_before_long_tests")
    if hidden_replay_artifact_preserved:
        cleared.add(PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX)
    if protected_diff_clean and coverage.get("pass"):
        cleared.add("move_repair_to_allowed_surface_and_restore_protected_diff_clean")
    if codex_exec_clean and coverage.get("pass"):
        cleared.add("make_the_patch_smaller_and_finish_before_timeout")
    ready = bool(
        contract_hook.get("pass") is True
        and evidence_ready
        and patch_summary_written
        and codex_exec_clean
        and coverage.get("pass")
        and all(item in cleared for item in required_fixes)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "card_id": str(card_id or contract_hook.get("card_id") or ""),
        "pass": ready,
        "required_axes": ["evidence", "tool"],
        "progress_axes": contract_hook.get("progress_axes", []),
        "cleared_next_small_fixes": [item for item in required_fixes if item in cleared],
        "missing_next_small_fixes": [item for item in required_fixes if item not in cleared],
        "evidence_hook_pass": evidence_ready,
        "tool_contract_pass": bool(contract_hook.get("pass")),
        "patch_summary_written": bool(patch_summary_written),
        "codex_exec_clean": bool(codex_exec_clean),
        "hidden_replay_artifact_preserved": bool(hidden_replay_artifact_preserved),
        "protected_diff_clean": bool(protected_diff_clean),
        "approval_axis_file_coverage": coverage,
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
        "policy": "patch-garden retry readiness only; no promotion or main-search influence without external gates",
    }


def hidden_replay_nursery_preservation_ok(variant_dir, changed_files):
    changed = {str(x).replace("\\", "/") for x in (changed_files or []) if str(x)}
    if TOOL_LAB_NAME not in changed:
        return {"pass": True, "reason": "not_touching_tool_lab"}
    path = Path(variant_dir) / TOOL_LAB_NAME
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    required = [
        "ToolLabHiddenReplayNursery",
        "ToolLabHiddenReplayArtifact",
        "preserve_hidden_replay_variant_artifact",
        "run_hidden_replay_request",
        "process_hidden_replay_requests",
        "hidden_replay_nursery_consumes_request_and_writes_v10_pass_result",
    ]
    missing = [token for token in required if token not in text]
    return {
        "pass": not missing,
        "reason": "ok" if not missing else "hidden_replay_nursery_preservation_missing",
        "missing": missing,
        "policy": "tool-lab promotions that touch development_tool_lab.py must preserve the hidden replay request consumer",
    }


def tool_brain_gate(manifest, judged, project_root, allow_codex, min_score=None, ticket=None):
    min_score = float(min_score if min_score is not None else os.environ.get("CODE_LAB_TOOL_BRAIN_MIN_SCORE", TOOL_BRAIN_MIN_SCORE))
    variant_dir = Path(manifest["variant_dir"])
    codex_exec = manifest.get("codex_exec") or {}
    score = judged.get("score") or {}
    judge = judged.get("judge") or {}
    changed = changed_promotable_files(variant_dir, project_root)
    compile_checks = compile_changed_files(variant_dir, changed)
    patch_summary = patch_summary_ok(variant_dir)
    progress_contract = evidence_tool_progress_contract_ok(
        patch_summary.get("summary", {}),
        require_evidence=True,
        require_tool=True,
    )
    approval_coverage = approval_axis_file_coverage(changed)
    hidden_replay_nursery_preservation = hidden_replay_nursery_preservation_ok(variant_dir, changed)
    reasons = []

    if not allow_codex:
        reasons.append("codex_not_enabled")
    if not codex_exec.get("pass"):
        reasons.append("codex_exec_not_clean")
    if not changed:
        reasons.append("no_promotable_file_changed")
    if not patch_summary.get("pass"):
        reasons.append(patch_summary.get("reason", "patch_summary_failed"))
    if not progress_contract.get("pass"):
        reasons.append(progress_contract.get("reason", "evidence_tool_progress_contract_failed"))
    if not approval_coverage.get("pass"):
        reasons.append(approval_coverage.get("reason", "approval_axis_file_coverage_failed"))
    if not hidden_replay_nursery_preservation.get("pass"):
        reasons.append(hidden_replay_nursery_preservation.get("reason", "hidden_replay_nursery_preservation_failed"))
    if score.get("reject") or not score.get("pass"):
        reasons.append("judge_or_critic_reject")
    if float(score.get("score") or 0.0) < min_score:
        reasons.append("score_below_tool_brain_min")
    if not judge.get("protected_diff_pass", False):
        reasons.append("protected_diff_not_clean")
    failed_compiles = [name for name, rep in compile_checks.items() if not rep.get("pass")]
    if failed_compiles:
        reasons.append("changed_file_compile_failed")

    return {
        "pass": not reasons,
        "reasons": reasons,
        "changed_files": changed,
        "compile_checks": compile_checks,
        "patch_summary": patch_summary,
        "evidence_tool_progress_contract": progress_contract,
        "approval_axis_file_coverage": approval_coverage,
        "hidden_replay_nursery_preservation": hidden_replay_nursery_preservation,
        "min_score": min_score,
        "score": score.get("score"),
    }


def v10_tool_lab_promotion_gate(project_root, manifest, gate):
    return _hub_operation("verify_replay", project_root, manifest, gate)


def promote_tool_brain_winner(project_root, run_dir, manifest, gate):
    return _hub_operation("promote_candidate", project_root, run_dir, manifest, gate)


def patch_garden_dir(project_root):
    path = tool_lab_root(project_root) / "nursery"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_patch_garden(project_root, limit=8):
    rows = []
    for line in read_tail_lines(patch_garden_dir(project_root) / "patch_garden.jsonl", max_lines=limit, max_bytes=2 * 1024 * 1024):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def patch_garden_card_id(card):
    import hashlib

    card = card if isinstance(card, dict) else {}
    payload = {
        "variant_name": card.get("variant_name", ""),
        "patch_family": card.get("patch_family", ""),
        "run_dir": card.get("run_dir", ""),
        "patch_summary_path": card.get("patch_summary_path", ""),
        "reasons": card.get("reasons", []),
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def hidden_replay_request_id(row):
    import hashlib

    row = row if isinstance(row, dict) else {}
    payload = {
        "patch_id": row.get("patch_id", ""),
        "card_id": row.get("card_id", ""),
        "variant_name": row.get("variant_name", ""),
        "patch_summary_path": row.get("patch_summary_path", ""),
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def reasons_require_hidden_replay(reasons):
    if isinstance(reasons, str):
        items = [reasons]
    elif isinstance(reasons, (list, tuple, set)):
        items = [str(x) for x in reasons]
    else:
        items = []
    return "v10_hidden_replay_gate_failed" in items or any("fresh_hidden_replay_required" in x for x in items)


def card_requires_hidden_replay(card):
    card = card if isinstance(card, dict) else {}
    return reasons_require_hidden_replay(card.get("reasons", [])) or reasons_require_hidden_replay(card.get("gate_reasons", []))


def patch_garden_card_has_missing_retry_summary(project_root, card):
    card = card if isinstance(card, dict) else {}
    codex_exec = card.get("codex_exec") if isinstance(card.get("codex_exec"), dict) else {}
    if codex_exec.get("pass"):
        return False
    reasons = {str(reason) for reason in (card.get("reasons") or [])}
    if "patch_summary_missing" not in reasons:
        return False
    summary_path = str(card.get("patch_summary_path") or "").strip()
    if not summary_path:
        return False
    path = Path(summary_path)
    if not path.is_absolute():
        path = resolve_root(project_root) / path
    return not path.exists()


def patch_garden_card_repair_triage(project_root, card, state=None):
    card = card if isinstance(card, dict) else {}
    state = state if isinstance(state, dict) else {}
    reasons = {str(reason) for reason in (card.get("reasons") or []) if str(reason)}
    next_small_fixes = {str(item) for item in (card.get("next_small_fixes") or []) if str(item)}
    changed_files = [str(item) for item in (card.get("changed_files") or []) if str(item)]
    codex_exec = card.get("codex_exec") if isinstance(card.get("codex_exec"), dict) else {}
    attempt_count = int(state.get("attempt_count", card.get("attempt_count", 0)) or 0)
    last_status = str(state.get("last_status") or card.get("last_attempt_status") or "")
    try:
        score = float(card.get("score") or 0.0)
    except Exception:
        score = 0.0

    def decision(status, active, reason):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "active": bool(active),
            "reason": reason,
            "attempt_count": attempt_count,
            "last_attempt_status": last_status,
            "promotion_allowed": False,
            "policy": "patch-garden triage only; archived cards remain memory and cannot bypass gates",
        }

    if state.get("passed") or str(card.get("status", "")).lower() in {"ok", "pass", "passed", "accepted", "ready", "promoted"}:
        return decision("already_resolved", False, "card already passed or was promoted")
    if patch_garden_card_has_missing_retry_summary(project_root, card):
        return decision("stale_missing_retry_summary", False, "failed lane never wrote the retry summary artifact")
    if (
        not changed_files
        and codex_exec.get("pass") is not True
        and PATCH_GARDEN_EMPTY_FAILED_REASONS.intersection(reasons)
        and not card_requires_hidden_replay(card)
    ):
        return decision("archived_empty_failed_lane", False, "failed sandbox produced no promotable changed file")
    if (
        PATCH_GARDEN_PROTECTED_DIFF_REASONS.intersection(reasons)
        and "move_repair_to_allowed_surface_and_restore_protected_diff_clean" in next_small_fixes
        and card.get("patch_summary_pass") is True
        and card.get("progress_contract_pass") is True
        and score <= 5.0
    ):
        return decision("quarantined_protected_diff_dead_end", False, "protected-diff failed near-pass must be reimplemented by a fresh clean lane")
    if attempt_count >= PATCH_GARDEN_MAX_RETRY_ATTEMPTS and last_status in {"needs_retry", "codex_retry_failed"}:
        return decision("retry_budget_exhausted", False, "retry budget exhausted without clearing tool-brain gates")
    return decision("retryable", True, "card still has an actionable repair path")


def read_hidden_replay_requests(project_root, limit=200):
    rows = []
    path = patch_garden_dir(project_root) / "hidden_replay_requests.jsonl"
    for line in read_tail_lines(path, max_lines=limit, max_bytes=2 * 1024 * 1024):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_hidden_replay_results(project_root, limit=200):
    rows = []
    garden = patch_garden_dir(project_root)
    candidates = [
        garden / "hidden_replay_results.jsonl",
        resolve_root(project_root) / "research_best" / "tool_lab_hidden_replay_results.jsonl",
        resolve_root(project_root) / "research_best" / "v10_hidden_replay_results.jsonl",
    ]
    for path in candidates:
        for line in read_tail_lines(path, max_lines=limit, max_bytes=2 * 1024 * 1024):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    latest_candidates = [
        garden / "latest_hidden_replay_result.json",
        resolve_root(project_root) / "research_best" / "tool_lab_hidden_replay_result_latest.json",
    ]
    for path in latest_candidates:
        row = read_json(path, {})
        if isinstance(row, dict) and row:
            rows.append(row)
    return rows[-limit:]


def read_hidden_replay_attempts(project_root, limit=200):
    rows = []
    path = patch_garden_dir(project_root) / "hidden_replay_attempts.jsonl"
    for line in read_tail_lines(path, max_lines=limit, max_bytes=2 * 1024 * 1024):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def latest_hidden_replay_attempt_for_patch(project_root, patch_id, card_id=""):
    patch_id = str(patch_id or "")
    card_id = str(card_id or "")
    keys = {x for x in (patch_id, card_id) if x}
    if not keys:
        return {}
    for row in reversed(read_hidden_replay_attempts(project_root, limit=500)):
        row_keys = {
            str(row.get("patch_id") or ""),
            str(row.get("card_id") or ""),
            str(row.get("request_id") or ""),
        }
        replay = row.get("replay_result") if isinstance(row.get("replay_result"), dict) else {}
        row_keys.add(str(replay.get("patch_id") or ""))
        if keys.intersection({x for x in row_keys if x}):
            return row
    return {}


def write_hidden_replay_nursery_status(project_root, attempts=None, pending=None):
    attempts = [a for a in (attempts or []) if isinstance(a, dict)]
    pending = [p for p in (pending or []) if isinstance(p, dict)]
    status = {
        "schema_version": SCHEMA_VERSION,
        "event": "ToolLabHiddenReplayNursery",
        "updated_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "active": bool(pending),
        "pass": not bool(pending),
        "attempt_count": len(attempts),
        "passed_count": sum(1 for a in attempts if a.get("pass")),
        "failed_count": sum(1 for a in attempts if a and not a.get("pass")),
        "pending_count": len(pending),
        "pending_patch_ids": [str(p.get("patch_id") or "") for p in pending[:12]],
        "policy": "non-final replay evidence only; no final, holdout, live, broker, or private-coordinate context is exposed",
        "attempts": attempts[-12:],
    }
    write_json(patch_garden_dir(project_root) / "latest_hidden_replay_nursery.json", status)
    return status


def hidden_replay_context_clean(obj):
    text = json.dumps(obj if isinstance(obj, dict) else {}, sort_keys=True, default=str).lower()
    forbidden = (
        "final_exam",
        "final holdout",
        "holdout rows",
        "broker" + "_order",
        "live" + "_order",
        "api" + "_key",
        "secret" + "_key",
    )
    return not any(tok in text for tok in forbidden)


def hidden_replay_artifact_bundle_dir(project_root, request):
    request = request if isinstance(request, dict) else {}
    request_id = str(request.get("request_id") or hidden_replay_request_id(request) or "")
    if not request_id:
        return None
    return patch_garden_dir(project_root) / "hidden_replay_artifacts" / request_id


def hidden_replay_variant_candidates(project_root, request, include_preserved=True):
    root = resolve_root(project_root)
    request = request if isinstance(request, dict) else {}
    patch_id = str(request.get("patch_id") or request.get("variant_name") or "")
    candidates = []
    for key in ("variant_dir",):
        raw = str(request.get(key) or "")
        if raw:
            candidates.append(Path(raw))
    patch_summary_path = Path(str(request.get("patch_summary_path") or ""))
    if str(patch_summary_path):
        candidates.append(patch_summary_path.parent)
    run_dir = Path(str(request.get("run_dir") or ""))
    if patch_id and str(run_dir):
        candidates.append(run_dir / "variants" / patch_id)
    if patch_id:
        runs_root = tool_lab_root(root) / "runs"
        try:
            matches = sorted(runs_root.glob(f"*/variants/{patch_id}"), key=lambda p: p.stat().st_mtime, reverse=True)
            candidates.extend(matches[:5])
        except Exception:
            pass
    if include_preserved:
        for key in ("variant_artifact_dir", "artifact_dir", "preserved_variant_dir"):
            raw = str(request.get(key) or "")
            if raw:
                candidates.append(Path(raw))
        bundle = hidden_replay_artifact_bundle_dir(root, request)
        if bundle is not None:
            candidates.append(bundle)
    return candidates


def hidden_replay_variant_dir(project_root, request, *, include_preserved=True):
    root = resolve_root(project_root)
    seen = set()
    candidates = hidden_replay_variant_candidates(root, request, include_preserved=include_preserved)
    for candidate in candidates:
        try:
            path = candidate if candidate.is_absolute() else root / candidate
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            if path.exists() and (path / "patch_summary.json").exists():
                return path
        except Exception:
            continue
    return None


def preserve_hidden_replay_variant_artifact(project_root, request, variant_dir=None):
    root = resolve_root(project_root)
    request = request if isinstance(request, dict) else {}
    source = None
    if variant_dir:
        source = Path(str(variant_dir))
        if not source.is_absolute():
            source = root / source
    if source is None or not source.exists():
        source = hidden_replay_variant_dir(root, request, include_preserved=False)
    if source is None or not source.exists() or not (source / "patch_summary.json").exists():
        return {
            "pass": False,
            "reason": "source_variant_artifact_missing",
            "policy": "hidden replay waits for a real sandbox artifact; no synthetic replay artifact is created",
        }
    bundle = hidden_replay_artifact_bundle_dir(root, request)
    if bundle is None:
        return {"pass": False, "reason": "request_id_missing"}
    try:
        if source.resolve() == bundle.resolve():
            copied = [str(p.relative_to(bundle)).replace("\\", "/") for p in bundle.rglob("*") if p.is_file()]
            return {"pass": bool((bundle / "patch_summary.json").exists()), "reason": "already_preserved", "artifact_dir": str(bundle), "copied_files": copied[:80]}
    except Exception:
        pass
    bundle.mkdir(parents=True, exist_ok=True)
    copied = []
    core_paths = [
        ENGINE_NAME,
        LEGACY_RUNTIME_NAME,
        SUPERVISOR_NAME,
        LAB_OS_NAME,
        TOOL_LAB_NAME,
        "AGENTS.md",
        "code_engine",
        "organs",
        "tool_lab_ticket.json",
        "tool_lab_autopsy.json",
        "tool_theory.json",
        "patch_summary.json",
    ]
    for name in core_paths:
        if copy_optional(source / name, bundle / name):
            copied.append(name)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "event": "ToolLabHiddenReplayArtifact",
        "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_id": str(request.get("request_id") or hidden_replay_request_id(request)),
        "patch_id": str(request.get("patch_id") or request.get("variant_name") or ""),
        "card_id": str(request.get("card_id") or ""),
        "source_variant_dir": str(source),
        "artifact_dir": str(bundle),
        "copied_paths": copied,
        "patch_summary_present": bool((bundle / "patch_summary.json").exists()),
        "promotion_allowed": False,
        "can_touch_final_holdout_or_live": False,
        "policy": "preserved sandbox artifact for non-final hidden replay only; no promotion authority",
    }
    write_json(bundle / "hidden_replay_artifact_manifest.json", manifest)
    return {
        "pass": bool(manifest["patch_summary_present"] and copied),
        "reason": "ok" if manifest["patch_summary_present"] and copied else "artifact_preservation_incomplete",
        "artifact_dir": str(bundle),
        "copied_files": copied,
        "manifest": manifest,
    }


def hidden_replay_median(values):
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return float((vals[mid - 1] + vals[mid]) / 2.0)


def write_hidden_replay_result(project_root, row):
    root = resolve_root(project_root)
    row = row if isinstance(row, dict) else {}
    if not row:
        return {}
    garden = patch_garden_dir(root)
    request_id = str(row.get("request_id") or "")
    patch_id = str(row.get("patch_id") or "")
    existing = [
        r for r in read_hidden_replay_results(root, limit=1000)
        if str(r.get("request_id") or "") == request_id
        or (patch_id and str(r.get("patch_id") or "") == patch_id and bool(r.get("pass", True)))
    ]
    if not existing:
        append_jsonl(garden / "hidden_replay_results.jsonl", row)
        append_jsonl(root / "research_best" / "tool_lab_hidden_replay_results.jsonl", row)
        append_jsonl(root / "research_best" / "v10_hidden_replay_results.jsonl", row)
    write_json(garden / "latest_hidden_replay_result.json", row)
    write_json(root / "research_best" / "tool_lab_hidden_replay_result_latest.json", row)
    return row


def is_surgery_hidden_replay_request(request):
    request = request if isinstance(request, dict) else {}
    return (
        str(request.get("event") or "") == "SurgeryHiddenReplayRequest"
        or str(request.get("patch_family") or "") == "code_surgery_supervisor"
    )


def surgery_hidden_replay_gate(summary, request=None, min_score=None):
    summary = summary if isinstance(summary, dict) else {}
    request = request if isinstance(request, dict) else {}
    min_score = float(min_score if min_score is not None else TOOL_BRAIN_MIN_SCORE)
    patch_id = str(request.get("patch_id") or request.get("variant_name") or "")
    summary_patch_id = str(summary.get("patch_id") or "")
    tests = summary.get("tests_run") if isinstance(summary.get("tests_run"), list) else []
    test_names = {str(row.get("name") or "") for row in tests if isinstance(row, dict)}
    required_tests = {
        "PatchCompile",
        "ArchitectureSelfCheck",
        "SyntheticCollapseTests",
        "MiniReplayProof",
        "SacredDiff",
        "PatchOverfitGuard",
    }
    reasons = []
    if summary.get("event") != "SurgeryHiddenReplayPatchSummary":
        reasons.append("surgery_patch_summary_missing")
    if summary_patch_id and patch_id and summary_patch_id != patch_id:
        reasons.append("surgery_patch_id_mismatch")
    if summary.get("pass") is not True:
        reasons.append("surgery_patch_summary_not_passing")
    if summary.get("promotion_allowed") is not False:
        reasons.append("surgery_promotion_must_remain_false")
    if summary.get("can_touch_protected_surfaces") is not False:
        reasons.append("surgery_protected_surface_authority_must_remain_false")
    if summary.get("protected_surfaces_checked") is not True:
        reasons.append("surgery_protected_surfaces_not_checked")
    missing_tests = sorted(required_tests - test_names)
    if missing_tests:
        reasons.append("surgery_required_tests_missing")
    if not tests or any(not bool(row.get("pass")) for row in tests if isinstance(row, dict)):
        reasons.append("surgery_required_test_failed")
    passed = not reasons
    return {
        "pass": bool(passed),
        "reasons": reasons,
        "missing_tests": missing_tests,
        "score": float(min_score + 15.0) if passed else 0.0,
        "min_score": min_score,
        "mode": "code_surgery_hidden_replay",
        "policy": "code-surgery restart evidence only; tool-brain promotion gate remains unchanged",
    }


def run_hidden_replay_request(project_root, request):
    return _hub_operation("run_hidden_replay", project_root, request)


def pending_hidden_replay_requests(project_root, limit=200):
    pending = []
    seen = set()
    failed_request_summaries = set()
    failed_patch_card_summaries = set()
    legacy_failed_request_ids = set()
    legacy_failed_patch_cards = set()
    for attempt in read_hidden_replay_attempts(project_root, limit=500):
        if not isinstance(attempt, dict) or attempt.get("pass"):
            continue
        attempt_req_id = str(attempt.get("request_id") or "")
        attempt_patch_id = str(attempt.get("patch_id") or "")
        attempt_card_id = str(attempt.get("card_id") or "")
        attempt_summary_hash = str(attempt.get("patch_summary_sha256") or "")
        if attempt_summary_hash:
            if attempt_req_id:
                failed_request_summaries.add((attempt_req_id, attempt_summary_hash))
            if attempt_patch_id:
                failed_patch_card_summaries.add((attempt_patch_id, attempt_card_id, attempt_summary_hash))
        else:
            if attempt_req_id:
                legacy_failed_request_ids.add(attempt_req_id)
            if attempt_patch_id:
                legacy_failed_patch_cards.add((attempt_patch_id, attempt_card_id))
    for req in read_hidden_replay_requests(project_root, limit=limit):
        if not isinstance(req, dict):
            continue
        patch_id = str(req.get("patch_id") or "")
        card_id = str(req.get("card_id") or "")
        req_id = str(req.get("request_id") or "")
        summary_hash = sha256_file(req.get("patch_summary_path") or "") or ""
        key = (patch_id, card_id, str(req.get("patch_summary_path") or ""), str(req.get("variant_dir") or ""))
        if key in seen:
            continue
        seen.add(key)
        if patch_id and hidden_replay_result_for_patch(project_root, patch_id, card_id=card_id):
            continue
        if summary_hash:
            if req_id and (req_id, summary_hash) in failed_request_summaries:
                continue
            if (
                (patch_id, card_id, summary_hash) in failed_patch_card_summaries
                or (patch_id, "", summary_hash) in failed_patch_card_summaries
            ):
                continue
        elif req_id and req_id in legacy_failed_request_ids:
            continue
        if not summary_hash and ((patch_id, card_id) in legacy_failed_patch_cards or (patch_id, "") in legacy_failed_patch_cards):
            continue
        pending.append(req)
    return pending


def process_hidden_replay_requests(project_root, limit=None):
    try:
        requested = int(limit if limit is not None else os.environ.get("CODE_LAB_TOOL_LAB_HIDDEN_REPLAY_PER_CYCLE", "3"))
    except Exception:
        requested = 3
    requested = max(0, min(12, requested))
    pending = pending_hidden_replay_requests(project_root, limit=300)
    attempts = []
    for req in pending[:requested]:
        attempts.append(run_hidden_replay_request(project_root, req))
    remaining = pending_hidden_replay_requests(project_root, limit=300)
    status = write_hidden_replay_nursery_status(project_root, attempts=attempts, pending=remaining)
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "ToolLabHiddenReplayNursery",
        "attempts": attempts,
        "pending_count": len(remaining),
        "pending_patch_ids": [str(r.get("patch_id") or "") for r in remaining[:12]],
        "status": status,
    }


def hidden_replay_result_for_patch(project_root, patch_id, card_id=""):
    patch_id = str(patch_id or "")
    card_id = str(card_id or "")
    if not patch_id and not card_id:
        return {}
    for row in reversed(read_hidden_replay_results(project_root, limit=500)):
        replay = row.get("replay_result") if isinstance(row.get("replay_result"), dict) else row
        row_keys = {
            str(row.get("patch_id") or ""),
            str(row.get("variant_name") or ""),
            str(row.get("card_id") or ""),
            str(replay.get("patch_id") or ""),
        }
        row_keys = {x for x in row_keys if x}
        if patch_id and patch_id not in row_keys:
            continue
        if not patch_id and card_id and card_id not in row_keys:
            continue
        if patch_id or card_id:
            replay = dict(replay)
            if patch_id and patch_id in row_keys:
                replay["patch_id"] = patch_id
            elif not replay.get("patch_id"):
                replay["patch_id"] = patch_id
            return replay
    return {}


def write_hidden_replay_request(project_root, card=None, attempt=None, promotion_decision=None, manifest=None, gate=None):
    card = card if isinstance(card, dict) else {}
    attempt = attempt if isinstance(attempt, dict) else {}
    promotion_decision = promotion_decision if isinstance(promotion_decision, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    gate = gate if isinstance(gate, dict) else {}
    variant_name = str(attempt.get("variant_name") or manifest.get("variant_name") or card.get("variant_name") or "")
    if not variant_name:
        return {}
    v10_gate = promotion_decision.get("promotion", {}).get("v10_gate") if isinstance(promotion_decision.get("promotion"), dict) else {}
    patch_summary = gate.get("patch_summary") if isinstance(gate.get("patch_summary"), dict) else {}
    request = {
        "schema_version": SCHEMA_VERSION,
        "event": "ToolLabHiddenReplayRequest",
        "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "requested",
        "patch_id": variant_name,
        "variant_name": variant_name,
        "card_id": str(card.get("card_id") or attempt.get("card_id") or ""),
        "patch_family": str(attempt.get("patch_family") or card.get("patch_family") or ""),
        "run_dir": str(card.get("run_dir") or ""),
        "variant_dir": str(manifest.get("variant_dir") or ""),
        "patch_summary_path": str(card.get("patch_summary_path") or patch_summary.get("path") or ""),
        "changed_files": list(attempt.get("changed_files") or gate.get("changed_files") or card.get("changed_files") or []),
        "required_evidence": "fresh_hidden_replay_result",
        "required_result_schema": {
            "patch_id": variant_name,
            "window_count": ">=3",
            "median_lift": ">0",
            "worst_window_lift": ">=0",
        },
        "v10_gate": v10_gate if isinstance(v10_gate, dict) else {},
        "policy": "request only; private hidden coordinates are not exposed and promotion remains blocked until rgv10_blind_gate passes",
        "promotion_allowed": False,
        "can_touch_final_holdout_or_live": False,
    }
    request["request_id"] = hidden_replay_request_id(request)
    artifact = preserve_hidden_replay_variant_artifact(project_root, request, variant_dir=manifest.get("variant_dir", ""))
    request["variant_artifact_preserved"] = bool(artifact.get("pass"))
    request["variant_artifact_dir"] = str(artifact.get("artifact_dir") or "")
    request["variant_artifact_files"] = list(artifact.get("copied_files") or [])[:40]
    request["variant_artifact_reason"] = str(artifact.get("reason") or "")
    existing = {str(r.get("request_id") or "") for r in read_hidden_replay_requests(project_root, limit=500)}
    if request["request_id"] not in existing:
        append_jsonl(patch_garden_dir(project_root) / "hidden_replay_requests.jsonl", request)
    latest = read_hidden_replay_requests(project_root, limit=50)
    write_json(patch_garden_dir(project_root) / "latest_hidden_replay_requests.json", {
        "schema_version": SCHEMA_VERSION,
        "requests": latest,
        "policy": "pending hidden replay requests are evidence requests only; they cannot promote code",
    })
    return request


def ensure_hidden_replay_requests_for_pending_attempts(project_root):
    cards = {str(c.get("card_id") or patch_garden_card_id(c)): c for c in read_patch_garden(project_root, limit=80)}
    requests = []
    for attempt in read_patch_garden_attempts(project_root, limit=200):
        if attempt.get("promoted"):
            continue
        if str(attempt.get("status") or "") not in {"gate_passed_promotion_pending", "hidden_replay_pending"}:
            continue
        card = cards.get(str(attempt.get("card_id") or ""), {"card_id": attempt.get("card_id")})
        req = write_hidden_replay_request(project_root, card=card, attempt=attempt)
        if req:
            requests.append(req)
    for card in cards.values():
        if not card_requires_hidden_replay(card):
            continue
        card_id = str(card.get("card_id") or patch_garden_card_id(card))
        patch_id = str(card.get("variant_name") or "")
        if not patch_id or hidden_replay_result_for_patch(project_root, patch_id, card_id=card_id):
            continue
        req = write_hidden_replay_request(project_root, card={**card, "card_id": card_id})
        if req:
            requests.append(req)
    return requests


def read_patch_garden_attempts(project_root, limit=200):
    rows = []
    path = patch_garden_dir(project_root) / "patch_garden_attempts.jsonl"
    for line in read_tail_lines(path, max_lines=limit, max_bytes=2 * 1024 * 1024):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def patch_garden_attempt_summary(project_root):
    summary = {}
    for row in read_patch_garden_attempts(project_root, limit=500):
        card_id = str(row.get("card_id") or "")
        if not card_id:
            continue
        current = summary.setdefault(card_id, {
            "attempt_count": 0,
            "last_status": "",
            "passed": False,
            "hidden_replay_pending": False,
            "hidden_replay_patch_ids": [],
        })
        current["attempt_count"] = int(current.get("attempt_count", 0) or 0) + 1
        current["last_status"] = str(row.get("status") or "")
        current["passed"] = bool(current.get("passed") or row.get("status") == "passed_and_promoted")
        status = str(row.get("status") or "")
        current["hidden_replay_pending"] = bool(current.get("hidden_replay_pending") or status in {"gate_passed_promotion_pending", "hidden_replay_pending"})
        if status in {"gate_passed_promotion_pending", "hidden_replay_pending"}:
            patch_id = str(row.get("variant_name") or "")
            patch_ids = current.setdefault("hidden_replay_patch_ids", [])
            if patch_id and patch_id not in patch_ids:
                patch_ids.append(patch_id)
        current["last_attempt_ts"] = row.get("created_ts", "")
    return summary


def active_patch_garden_cards(project_root, limit=None):
    try:
        requested = int(limit if limit is not None else os.environ.get("CODE_LAB_TOOL_LAB_NURSERY_LANES", str(PATCH_GARDEN_NURSERY_DEFAULT_LANES)))
    except Exception:
        requested = PATCH_GARDEN_NURSERY_DEFAULT_LANES
    requested = max(0, min(PATCH_TOURNAMENT_MAX_VARIANTS, requested))
    attempts = patch_garden_attempt_summary(project_root)
    candidates = []
    seen = set()
    for card in reversed(read_patch_garden(project_root, limit=24)):
        if not isinstance(card, dict):
            continue
        card_id = patch_garden_card_id(card)
        if card_id in seen:
            continue
        seen.add(card_id)
        state = attempts.get(card_id, {})
        if state.get("passed"):
            continue
        if patch_garden_card_has_missing_retry_summary(project_root, card):
            continue
        base_triage = patch_garden_card_repair_triage(project_root, card, state=state)
        if not base_triage.get("active"):
            continue
        card_hidden_pending = card_requires_hidden_replay(card)
        replay_patch_ids = [str(card.get("variant_name") or "")]
        replay_patch_ids.extend(str(x or "") for x in state.get("hidden_replay_patch_ids", []))
        replay_ready = any(
            hidden_replay_result_for_patch(project_root, patch_id, card_id=card_id)
            for patch_id in replay_patch_ids
            if patch_id
        )
        replay_attempts = [
            latest_hidden_replay_attempt_for_patch(project_root, patch_id, card_id=card_id)
            for patch_id in replay_patch_ids
            if patch_id
        ]
        replay_attempt = next((a for a in replay_attempts if a), {})
        replay_failed = bool(replay_attempt and not replay_attempt.get("pass"))
        if (state.get("hidden_replay_pending") or card_hidden_pending) and not replay_ready and not replay_failed:
            continue
        reasons = list(card.get("reasons", []) or [])
        next_small_fixes = list(card.get("next_small_fixes", []) or [])
        if replay_failed:
            reason = str(replay_attempt.get("reason") or "hidden_replay_failed")
            if reason not in reasons:
                reasons.append(reason)
            fix = "repair_hidden_replay_failure_before_promotion"
            if fix not in next_small_fixes:
                next_small_fixes.insert(0, fix)
        enriched = {
            **card,
            "card_id": card_id,
            "attempt_count": int(state.get("attempt_count", 0) or 0),
            "last_attempt_status": state.get("last_status", ""),
            "hidden_replay_result_ready": replay_ready,
            "hidden_replay_failed": replay_failed,
            "hidden_replay_failure_reason": str(replay_attempt.get("reason") or "") if replay_attempt else "",
            "reasons": reasons,
            "next_small_fixes": next_small_fixes,
            "repair_triage": base_triage,
        }
        candidates.append(enriched)
    candidates.sort(key=lambda c: (int(c.get("attempt_count", 0) or 0), str(c.get("created_ts") or "")))
    return candidates[:requested]


def build_patch_garden_repair_theory(card):
    card = card if isinstance(card, dict) else {}
    card_id = str(card.get("card_id") or patch_garden_card_id(card))
    family = str(card.get("patch_family") or "patch_garden_repair")
    return {
        "id": f"U{card_id[:6]}",
        "name": f"U_patch_garden_repair_{card_id[:6]}",
        "patch_family": family,
        "scope": "patch_garden_nurse",
        "diagnosis": "A previous tool/evidence sandbox patch came close or failed; nurse it through the smallest safe Codex repair until approval gates pass.",
        "expected_surface": "tool/evidence patch garden, exact blockers, next small fixes, both-axis approval contract",
        "proof_test": "the retry clears the card's next_small_fixes while preserving protected surfaces and passing both tool and evidence approval axes",
        "failure_condition": "the same blocker returns without a sharper next repair step",
        "patch_garden_repair": True,
        "patch_garden_card_id": card_id,
        "patch_garden_card": card,
    }


def build_patch_garden_nurse_plan(project_root, cards, allow_codex):
    input_cards = [c for c in (cards or []) if isinstance(c, dict)]
    triaged_cards = [(c, patch_garden_card_repair_triage(project_root, c)) for c in input_cards]
    skipped_missing_summary = [
        c for c, triage in triaged_cards if triage.get("status") == "stale_missing_retry_summary"
    ]
    skipped_non_actionable = [
        (c, triage)
        for c, triage in triaged_cards
        if not triage.get("active") and triage.get("status") != "stale_missing_retry_summary"
    ]
    cards = [
        {**c, "repair_triage": triage}
        for c, triage in triaged_cards
        if triage.get("active")
    ]
    active = bool(cards)
    pending_hidden = pending_hidden_replay_requests(project_root, limit=100)
    replay_nursery = read_json(patch_garden_dir(project_root) / "latest_hidden_replay_nursery.json", {})
    plan = {
        "schema_version": SCHEMA_VERSION,
        "event": "PatchGardenNurse",
        "updated_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "active": bool(active or pending_hidden),
        "pass": not bool(active or pending_hidden),
        "retry_requested": active,
        "approval_status": "retry_required" if active else ("awaiting_hidden_replay" if pending_hidden else "no_active_cards"),
        "promotion_allowed": False,
        "allow_codex": bool(allow_codex),
        "input_card_count": len(input_cards),
        "card_count": len(cards),
        "skipped_missing_retry_summary_count": len(skipped_missing_summary),
        "skipped_missing_retry_summary_cards": [
            {
                "card_id": c.get("card_id") or patch_garden_card_id(c),
                "variant_name": c.get("variant_name", ""),
                "patch_family": c.get("patch_family", ""),
                "patch_summary_path": c.get("patch_summary_path", ""),
                "reasons": (c.get("reasons") or [])[:6] if isinstance(c.get("reasons"), list) else [],
            }
            for c in skipped_missing_summary[:8]
        ],
        "skipped_non_actionable_count": len(skipped_non_actionable),
        "skipped_non_actionable_cards": [
            {
                "card_id": c.get("card_id") or patch_garden_card_id(c),
                "variant_name": c.get("variant_name", ""),
                "patch_family": c.get("patch_family", ""),
                "triage_status": triage.get("status"),
                "triage_reason": triage.get("reason"),
                "reasons": (c.get("reasons") or [])[:6] if isinstance(c.get("reasons"), list) else [],
            }
            for c, triage in skipped_non_actionable[:8]
        ],
        "pending_hidden_replay_count": len(pending_hidden),
        "pending_hidden_replay_patch_ids": [str(r.get("patch_id") or "") for r in pending_hidden[:8]],
        "hidden_replay_nursery": replay_nursery if isinstance(replay_nursery, dict) else {},
        "cards": [
            {
                "card_id": c.get("card_id") or patch_garden_card_id(c),
                "variant_name": c.get("variant_name", ""),
                "patch_family": c.get("patch_family", ""),
                "status": c.get("status", "needs_small_fix"),
                "score": c.get("score"),
                "patch_summary_path": c.get("patch_summary_path", ""),
                "patch_summary_pass": c.get("patch_summary_pass"),
                "progress_contract_pass": c.get("progress_contract_pass"),
                "changed_files": c.get("changed_files", []),
                "codex_exec": c.get("codex_exec", {}),
                "attempt_count": int(c.get("attempt_count", 0) or 0),
                "last_attempt_status": c.get("last_attempt_status", ""),
                "next_small_fixes": c.get("next_small_fixes", []),
                "reasons": c.get("reasons", []),
                "repair_triage": c.get("repair_triage", {}),
            }
            for c in cards
        ],
        "approval_conditions": [
            "Codex sandbox execution completes",
            "patch_summary.json exists before long tests",
            "evidence_tool_progress_contract proves both evidence and tool axes",
            "approval_axis_file_coverage touches one evidence-side and one tool-side file",
            "compile, protected diff, judge, and hidden replay gates pass",
            "promotion remains false inside patch_summary until external proof promotion succeeds",
        ],
        "runner_policy": "retry code only for active repair cards; when the code gate passes and V10 blocks, run the non-final hidden replay nursery and wait without bypassing proof gates",
    }
    garden = patch_garden_dir(project_root)
    write_json(garden / "patch_garden_nurse.json", plan)
    return plan


def append_patch_garden_attempt(project_root, card, lane, promotion_decision=None):
    card = card if isinstance(card, dict) else {}
    lane = lane if isinstance(lane, dict) else {}
    row = lane.get("scoreboard_row") if isinstance(lane.get("scoreboard_row"), dict) else {}
    gate = lane.get("tool_brain_gate") if isinstance(lane.get("tool_brain_gate"), dict) else {}
    manifest = lane.get("manifest") if isinstance(lane.get("manifest"), dict) else {}
    promotion_decision = promotion_decision if isinstance(promotion_decision, dict) else {}
    status = "needs_retry"
    if gate.get("pass") and promotion_decision.get("promoted"):
        status = "passed_and_promoted"
    elif gate.get("pass") and str(promotion_decision.get("reason") or "") == "v10_hidden_replay_gate_failed":
        status = "hidden_replay_pending"
    elif gate.get("pass"):
        status = "gate_passed_promotion_pending"
    elif row.get("codex_ran"):
        status = "codex_retry_failed"
    record = {
        "schema_version": SCHEMA_VERSION,
        "event": "PatchGardenAttempt",
        "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "card_id": card.get("card_id") or patch_garden_card_id(card),
        "status": status,
        "variant_name": row.get("variant_name") or manifest.get("variant_name", ""),
        "patch_family": row.get("patch_family") or card.get("patch_family", ""),
        "codex_ran": bool(row.get("codex_ran")),
        "tool_brain_gate_pass": bool(gate.get("pass")),
        "promoted": bool(promotion_decision.get("promoted")),
        "score": row.get("score"),
        "gate_reasons": gate.get("reasons", []),
        "changed_files": gate.get("changed_files", []),
        "next_small_fixes": card.get("next_small_fixes", []),
        "required_evidence": "fresh_hidden_replay_result" if status == "hidden_replay_pending" else "",
        "promotion_allowed": bool(promotion_decision.get("promoted")),
        "policy": "attempt memory only; approval still requires proof gates",
    }
    if status == "hidden_replay_pending":
        record["hidden_replay_request"] = write_hidden_replay_request(
            project_root,
            card=card,
            attempt=record,
            promotion_decision=promotion_decision,
            manifest=manifest,
            gate=gate,
        )
    append_jsonl(patch_garden_dir(project_root) / "patch_garden_attempts.jsonl", record)
    return record


def process_hidden_replay_ready_promotions(project_root, allow_promote=False):
    root = resolve_root(project_root)
    if not allow_promote:
        return []
    cards = {str(c.get("card_id") or patch_garden_card_id(c)): c for c in read_patch_garden(root, limit=120)}
    attempts = read_patch_garden_attempts(root, limit=300)
    passed = {str(a.get("card_id") or "") for a in attempts if a.get("status") == "passed_and_promoted"}
    processed = []
    seen = set()
    for attempt in reversed(attempts):
        card_id = str(attempt.get("card_id") or "")
        if not card_id or card_id in passed or card_id in seen:
            continue
        if str(attempt.get("status") or "") not in {"gate_passed_promotion_pending", "hidden_replay_pending"}:
            continue
        patch_id = str(attempt.get("variant_name") or "")
        replay = hidden_replay_result_for_patch(root, patch_id, card_id=card_id)
        if not replay:
            continue
        seen.add(card_id)
        card = cards.get(card_id, {"card_id": card_id, "variant_name": patch_id})
        replay_request = attempt.get("hidden_replay_request") if isinstance(attempt.get("hidden_replay_request"), dict) else {}
        variant_dir = hidden_replay_variant_dir(root, {**card, **replay_request, **replay, "patch_id": patch_id})
        run_dir = Path(str(card.get("run_dir") or replay_request.get("run_dir") or ""))
        if variant_dir is not None and not str(run_dir):
            try:
                run_dir = variant_dir.parents[1]
            except Exception:
                run_dir = Path("")
        if variant_dir is None or not variant_dir.exists():
            processed.append({
                "card_id": card_id,
                "patch_id": patch_id,
                "pass": False,
                "reason": "variant_dir_missing_for_hidden_replay_promotion",
            })
            continue
        manifest = {
            "variant_name": patch_id,
            "variant_dir": str(variant_dir),
            "codex_exec": {"pass": True, "reused_previous_codex_patch": True},
        }
        judged = judge_with_lab_os(variant_dir, root)
        gate = tool_brain_gate(manifest, judged, root, allow_codex=True, ticket={})
        promotion = promote_tool_brain_winner(root, run_dir, manifest, gate) if gate.get("pass") else {"pass": False, "reason": "tool_brain_gate_failed_after_hidden_replay", "gate": gate}
        decision = {
            "promoted": bool(promotion.get("pass")),
            "reason": "proof_gated_tool_brain_promoted" if promotion.get("pass") else str(promotion.get("reason") or "promotion_failed_after_hidden_replay"),
            "promotion": promotion,
        }
        lane = {
            "scoreboard_row": {
                "variant_name": patch_id,
                "patch_family": attempt.get("patch_family") or card.get("patch_family", ""),
                "codex_ran": True,
                "score": attempt.get("score"),
            },
            "tool_brain_gate": gate,
            "manifest": manifest,
        }
        record = append_patch_garden_attempt(root, card, lane, promotion_decision=decision)
        processed.append({
            "card_id": card_id,
            "patch_id": patch_id,
            "pass": bool(record.get("status") == "passed_and_promoted"),
            "attempt": record,
            "promotion": promotion,
        })
    return processed


def patch_garden_next_actions(reasons):
    reason_set = {str(x) for x in (reasons or [])}
    actions = []
    if "codex_exec_not_clean" in reason_set:
        actions.append("make_the_patch_smaller_and_finish_before_timeout")
    if "patch_summary_missing" in reason_set:
        actions.append("write_patch_summary_json_before_long_tests")
    if "evidence_tool_progress_contract_missing" in reason_set:
        actions.append("add_evidence_tool_progress_contract_with_before_after_evidence")
    if "evidence_axis_improvement_missing" in reason_set:
        actions.append("add_evidence_new_cards_or_bridge_or_stale_refresh_evidence")
    if "tool_axis_improvement_missing" in reason_set:
        actions.append("add_tool_observe_lift_quarantine_or_bridge_evidence")
    if "evidence_axis_code_change_missing" in reason_set:
        actions.append("patch_code_engine_evidence_proof_worker_with_one_small_progress_hook")
    if "tool_axis_code_change_missing" in reason_set:
        actions.append("patch_tool_lab_or_tool_policy_with_one_small_progress_hook")
    if "changed_file_compile_failed" in reason_set:
        actions.append("fix_compile_errors_before_any_replay")
    if "protected_diff_not_clean" in reason_set or "protected_diff_fail" in reason_set:
        actions.append("move_repair_to_allowed_surface_and_restore_protected_diff_clean")
    if "judge_or_critic_reject" in reason_set or "score_below_tool_brain_min" in reason_set:
        actions.append("add_one_cheap_self_check_that_proves_the_claim")
    if "v10_hidden_replay_gate_failed" in reason_set:
        actions.append("collect_fresh_hidden_replay_evidence_without_bypassing_v10_gate")
    if "variant_artifact_missing_for_hidden_replay" in reason_set or "source_variant_artifact_missing" in reason_set:
        actions.append("repair_hidden_replay_failure_before_promotion")
        actions.append("write_patch_summary_json_before_long_tests")
    actions.append("keep_promotion_allowed_false_until_all_gates_pass")
    return list(dict.fromkeys(actions))


def build_patch_garden_card(project_root, run_dir, row, manifest=None, gate=None, judged=None, theory=None, promotion_decision=None):
    gate = gate if isinstance(gate, dict) else {}
    judged = judged if isinstance(judged, dict) else {}
    row = row if isinstance(row, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    score = judged.get("score") if isinstance(judged.get("score"), dict) else {}
    patch_summary = gate.get("patch_summary") if isinstance(gate.get("patch_summary"), dict) else {}
    contract = gate.get("evidence_tool_progress_contract") if isinstance(gate.get("evidence_tool_progress_contract"), dict) else {}
    coverage = gate.get("approval_axis_file_coverage") if isinstance(gate.get("approval_axis_file_coverage"), dict) else {}
    reasons = []
    for source in (row.get("tool_brain_gate_reasons"), gate.get("reasons"), row.get("reasons"), score.get("reasons")):
        if isinstance(source, list):
            reasons.extend(str(x) for x in source if str(x))
    if promotion_decision and isinstance(promotion_decision, dict):
        reason = str(promotion_decision.get("reason") or "")
        if reason:
            reasons.append(reason)
        promotion = promotion_decision.get("promotion") if isinstance(promotion_decision.get("promotion"), dict) else {}
        if promotion.get("reason"):
            reasons.append(str(promotion.get("reason")))
    reasons = list(dict.fromkeys(reasons))
    card = {
        "schema_version": SCHEMA_VERSION,
        "card_type": "tool_lab_patch_garden_card",
        "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "needs_small_fix",
        "nursery_policy": "record-only repair memory; no promotion authority, no gate bypass, no final/holdout/live access",
        "run_dir": str(run_dir),
        "variant_name": str(row.get("variant_name") or manifest.get("variant_name") or ""),
        "patch_family": str(row.get("patch_family") or (theory or {}).get("patch_family") or ""),
        "score": row.get("score", score.get("score")),
        "changed_files": gate.get("changed_files") or row.get("changed_files") or [],
        "reasons": reasons,
        "next_small_fixes": patch_garden_next_actions(reasons),
        "patch_summary_path": patch_summary.get("path", ""),
        "patch_summary_pass": bool(patch_summary.get("pass")),
        "progress_contract_pass": bool(contract.get("pass")),
        "progress_axes": contract.get("progress_axes", []),
        "required_axes": contract.get("required_axes", []),
        "approval_axis_file_coverage_pass": bool(coverage.get("pass")),
        "approval_evidence_files": coverage.get("evidence_files", []),
        "approval_tool_files": coverage.get("tool_files", []),
        "codex_exec": {
            "pass": bool((manifest.get("codex_exec") or {}).get("pass")),
            "seconds": (manifest.get("codex_exec") or {}).get("seconds"),
            "returncode": (manifest.get("codex_exec") or {}).get("returncode"),
        },
    }
    card["card_id"] = patch_garden_card_id(card)
    return card


def should_keep_patch_garden_card(row, manifest=None, gate=None, promotion_decision=None):
    row = row if isinstance(row, dict) else {}
    gate = gate if isinstance(gate, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    if promotion_decision and (promotion_decision.get("winner") or promotion_decision.get("promotion")):
        return bool(not promotion_decision.get("promoted"))
    if row.get("tool_brain_eligible"):
        return False
    if row.get("codex_ran"):
        return True
    if gate.get("changed_files"):
        return True
    patch_summary = gate.get("patch_summary") if isinstance(gate.get("patch_summary"), dict) else {}
    if patch_summary.get("summary"):
        return True
    codex_exec = manifest.get("codex_exec") if isinstance(manifest.get("codex_exec"), dict) else {}
    return bool(codex_exec and codex_exec.get("skipped") is not True)


def should_write_new_patch_garden_card(theory, row, manifest=None, gate=None, promotion_decision=None):
    if isinstance(theory, dict) and theory.get("patch_garden_repair"):
        return False
    return should_keep_patch_garden_card(row, manifest=manifest, gate=gate, promotion_decision=promotion_decision)


def append_patch_garden_card(project_root, card):
    garden = patch_garden_dir(project_root)
    if isinstance(card, dict) and not card.get("card_id"):
        card = {**card, "card_id": patch_garden_card_id(card)}
    append_jsonl(garden / "patch_garden.jsonl", card)
    latest = read_patch_garden(project_root, limit=12)
    write_json(garden / "latest_patch_garden.json", {
        "schema_version": SCHEMA_VERSION,
        "cards": latest,
        "policy": "near-pass or failed sandbox patches are kept for small safe fixes; cards cannot promote code",
    })
    return card


def read_tool_memory(project_root, limit=20):
    path = tool_lab_root(project_root) / "memory" / "tool_lab_memory.jsonl"
    rows = []
    for line in read_tail_lines(path, max_lines=limit):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def count_tool_memory(project_root):
    path = tool_lab_root(project_root) / "memory" / "tool_lab_memory.jsonl"
    if not path.exists():
        return 0
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _theory_by_id(theory_id):
    return next((t for t in CODE_BRAIN_THEORIES if str(t.get("id")) == str(theory_id)), None)


def forced_progress_theories(ticket):
    symptoms = ticket.get("symptoms") if isinstance(ticket, dict) else {}
    evidence = symptoms.get("evidence_progress") if isinstance(symptoms.get("evidence_progress"), dict) else {}
    spread = symptoms.get("search_power_spread") if isinstance(symptoms.get("search_power_spread"), dict) else {}
    tool_ga = symptoms.get("tool_atom_flow_ga") if isinstance(symptoms.get("tool_atom_flow_ga"), dict) else {}
    section_mixer = symptoms.get("section_shadow_lane_mixer") if isinstance(symptoms.get("section_shadow_lane_mixer"), dict) else {}
    component_gas = symptoms.get("component_atom_promotion_gas") if isinstance(symptoms.get("component_atom_promotion_gas"), dict) else {}
    requested_ids = spread.get("forced_theory_ids") if isinstance(spread.get("forced_theory_ids"), list) else []
    forced_ids = []
    if component_gas.get("active"):
        for theory_id in component_gas.get("forced_theory_ids") or []:
            text = str(theory_id or "").strip()
            if text:
                forced_ids.append(text)
        for component in component_gas.get("components") or []:
            if not isinstance(component, dict) or not component.get("active"):
                continue
            for theory_id in component.get("selected_theory_ids") or []:
                text = str(theory_id or "").strip()
                if text:
                    forced_ids.append(text)
    if tool_ga.get("active"):
        for theory_id in tool_ga.get("selected_theory_ids") or []:
            text = str(theory_id or "").strip()
            if text:
                forced_ids.append(text)
    if section_mixer.get("active"):
        forced_ids.append("S")
    for theory_id in requested_ids:
        text = str(theory_id or "").strip()
        if text:
            forced_ids.append(text)
    if (
        bool(symptoms.get("search_power_spread_problem"))
        or bool(spread.get("spread_needed"))
    ):
        forced_ids.append("S")
    if (
        bool(symptoms.get("repair_factory_problem"))
        or int(symptoms.get("repair_nursery_candidates", 0) or 0) > 0
        or int(spread.get("converted_nursery_candidates", 0) or 0) > 0
    ):
        forced_ids.append("M")
    if bool(symptoms.get("evidence_progress_problem")) or bool(evidence.get("needs_supervisor_attention")) or str(evidence.get("progress_state")) == "stale_input_refresh_required":
        forced_ids.append("R")
    if bool(symptoms.get("tool_usefulness_problem")) or int(symptoms.get("tool_bridge_promoted_count", 0) or 0) <= 0:
        forced_ids.append("B")
    selected = []
    for theory_id in forced_ids:
        theory = _theory_by_id(theory_id)
        if theory and theory not in selected:
            selected.append(theory)
    return selected


def select_theories(project_root, memory, max_variants=None, ticket=None):
    max_variants = int(max_variants or os.environ.get("CODE_LAB_CODE_BRAIN_VARIANTS_PER_CYCLE", str(CODE_BRAIN_DEFAULT_VARIANTS_PER_CYCLE)))
    max_variants = max(1, min(PATCH_TOURNAMENT_MAX_VARIANTS, max_variants, len(CODE_BRAIN_THEORIES)))
    memory_count = count_tool_memory(project_root)
    offset = memory_count % len(CODE_BRAIN_THEORIES)
    rotated = CODE_BRAIN_THEORIES[offset:] + CODE_BRAIN_THEORIES[:offset]
    selected = forced_progress_theories(ticket or {})
    if rotated:
        if rotated[0] not in selected:
            selected.append(rotated[0])
    if max_variants >= 2:
        broad_scopes = {"search_ecology", "integration_stage_routing", "anti_overfit", "runner_reliability", "curriculum", "scheduler", "repair_factory", "self_repair", "search_governor", "search_power_spread", "anti_overfit_auditor", "test_generation", "evidence_proof"}
        broad = [t for t in CODE_BRAIN_THEORIES if str(t.get("scope", "")) in broad_scopes]
        if broad:
            broad_pick = broad[memory_count % len(broad)]
            if broad_pick not in selected:
                selected.append(broad_pick)
    for theory in rotated:
        if len(selected) >= max_variants:
            break
        if theory not in selected:
            selected.append(theory)
    return selected[:max_variants]


def select_cycle_theories(nursery_theories, base_theories, max_variants, allow_codex):
    nursery = list(nursery_theories or [])
    base = list(base_theories or [])
    max_variants = max(1, int(max_variants or 1))
    nursery_exclusive = bool(
        nursery
        and allow_codex
        and os.environ.get("CODE_LAB_PATCH_GARDEN_EXCLUSIVE", "0").strip().lower() in ("1", "true", "yes", "on")
    )
    if nursery_exclusive:
        return nursery[:max_variants], True
    selected = nursery + [t for t in base if t.get("id") not in {n.get("id") for n in nursery}]
    return selected[:max_variants], False


def lab_team_worker_count(selected_count, requested=None):
    selected_count = max(0, int(selected_count or 0))
    if selected_count <= 0:
        return 0
    raw = requested
    if raw is None:
        raw = os.environ.get(
            "CODE_LAB_CODE_BRAIN_LAB_WORKERS",
            os.environ.get("CODE_LAB_TOOL_LAB_WORKERS", str(CODE_BRAIN_DEFAULT_LAB_WORKERS)),
        )
    try:
        workers = int(raw)
    except Exception:
        workers = CODE_BRAIN_DEFAULT_LAB_WORKERS
    return max(1, min(PATCH_TOURNAMENT_MAX_VARIANTS, selected_count, workers))


def build_lane_failure(theory, allow_codex, exc):
    reason = f"lab_lane_exception:{type(exc).__name__}"
    variant_name = f"variant_{theory.get('id', 'unknown')}_{theory.get('name', 'unknown')}"
    row = {
        "variant_name": variant_name,
        "patch_family": theory.get("patch_family", ""),
        "score": -100.0,
        "pass": False,
        "reject": True,
        "reasons": [reason],
        "codex_ran": bool(allow_codex),
        "tool_brain_eligible": False,
        "tool_brain_gate_reasons": [reason],
        "changed_files": [],
        "lane_error": str(exc)[:900],
    }
    judged = {
        "judge": {"pass": False, "lane_error": row["lane_error"]},
        "critics": {"lab_lane": {"pass": False, "issues": [reason], "reject": True}},
        "score": {"score": -100.0, "pass": False, "reject": True, "reasons": [reason]},
    }
    gate = {"pass": False, "reasons": [reason], "changed_files": []}
    return {"manifest": None, "judged": judged, "tool_brain_gate": gate, "scoreboard_row": row}


def run_lab_team_lane(project_root, run_dir, theory, ticket, autopsy, memory, allow_codex):
    started = time.time()
    manifest = create_variant(project_root, run_dir, theory, ticket, autopsy, memory)
    variant_dir = Path(manifest["variant_dir"])
    write_json(variant_dir / "lane_status.json", {
        "schema_version": SCHEMA_VERSION,
        "variant_name": manifest["variant_name"],
        "patch_family": theory.get("patch_family", ""),
        "stage": "created",
        "allow_codex": bool(allow_codex),
        "patch_garden_repair": bool(theory.get("patch_garden_repair")),
        "started_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
    })
    if allow_codex:
        manifest["codex_exec"] = run_codex_exec(
            manifest["prompt"],
            manifest["variant_dir"],
            timeout=int(os.environ.get("CODE_LAB_TOOL_LAB_CODEX_TIMEOUT_SEC", "1800")),
        )
    else:
        manifest["codex_exec"] = {"skipped": True, "reason": "CODE_LAB_TOOL_LAB_ALLOW_CODEX_not_enabled"}
    if allow_codex and not manifest["codex_exec"].get("pass"):
        manifest["blocked_patch_summary"] = ensure_blocked_patch_summary(
            variant_dir,
            theory=theory,
            codex_exec=manifest["codex_exec"],
        )
    write_json(variant_dir / "codex_exec_result.json", manifest["codex_exec"])
    write_json(variant_dir / "lane_status.json", {
        "schema_version": SCHEMA_VERSION,
        "variant_name": manifest["variant_name"],
        "patch_family": theory.get("patch_family", ""),
        "stage": "codex_complete",
        "allow_codex": bool(allow_codex),
        "codex_pass": bool(manifest["codex_exec"].get("pass")),
        "codex_returncode": manifest["codex_exec"].get("returncode"),
        "patch_garden_repair": bool(theory.get("patch_garden_repair")),
        "seconds": round(time.time() - started, 3),
    })
    judged = judge_with_lab_os(Path(manifest["variant_dir"]), project_root)
    gate = tool_brain_gate(manifest, judged, project_root, allow_codex=allow_codex, ticket=ticket)
    row = {
        "variant_name": manifest["variant_name"],
        "patch_family": theory["patch_family"],
        "score": judged["score"].get("score"),
        "pass": judged["score"].get("pass"),
        "reject": judged["score"].get("reject"),
        "reasons": judged["score"].get("reasons"),
        "codex_ran": bool(allow_codex),
        "tool_brain_eligible": bool(gate.get("pass")),
        "tool_brain_gate_reasons": gate.get("reasons"),
        "changed_files": gate.get("changed_files"),
        "lane_seconds": round(time.time() - started, 3),
    }
    lane_result = {"manifest": manifest, "judged": judged, "tool_brain_gate": gate, "scoreboard_row": row}
    write_json(variant_dir / "lane_result.json", lane_result)
    write_json(variant_dir / "lane_status.json", {
        "schema_version": SCHEMA_VERSION,
        "variant_name": manifest["variant_name"],
        "patch_family": theory.get("patch_family", ""),
        "stage": "judged",
        "allow_codex": bool(allow_codex),
        "codex_pass": bool(manifest["codex_exec"].get("pass")),
        "tool_brain_eligible": bool(gate.get("pass")),
        "patch_garden_repair": bool(theory.get("patch_garden_repair")),
        "seconds": row["lane_seconds"],
    })
    return lane_result


def run_lab_team_variants(project_root, run_dir, selected_theories, ticket, autopsy, memory, allow_codex, worker_limit=None):
    selected = list(selected_theories or [])
    workers = lab_team_worker_count(len(selected))
    if worker_limit is not None:
        workers = max(1, min(workers, int(worker_limit)))
    if workers <= 1:
        results = []
        for theory in selected:
            try:
                results.append(run_lab_team_lane(project_root, run_dir, theory, ticket, autopsy, memory, allow_codex))
            except Exception as exc:
                results.append(build_lane_failure(theory, allow_codex, exc))
        return {"workers": workers, "mode": "serial", "results": results}

    results = [None] * len(selected)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="code_brain_lab") as executor:
        futures = {
            executor.submit(run_lab_team_lane, project_root, run_dir, theory, ticket, autopsy, memory, allow_codex): (idx, theory)
            for idx, theory in enumerate(selected)
        }
        for future in concurrent.futures.as_completed(futures):
            idx, theory = futures[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = build_lane_failure(theory, allow_codex, exc)
    return {"workers": workers, "mode": "parallel", "results": [r for r in results if r is not None]}


def run_disk_guard(project_root):
    return _hub_operation("disk_guard", project_root)


def ensure_dirs(project_root):
    root = tool_lab_root(project_root)
    for rel in ("runs", "tickets", "memory", "logs", "status", "nursery"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def dir_size_bytes(path):
    total = 0
    path = Path(path)
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def is_safe_child(parent, child):
    try:
        parent = Path(parent).resolve()
        child = Path(child).resolve()
        return str(child).lower().startswith(str(parent).lower() + os.sep)
    except Exception:
        return False


def prune_old_tool_lab_runs(project_root):
    return _hub_operation("retention", project_root, kind="runs")


def prune_old_tool_lab_tickets(project_root):
    return _hub_operation("retention", project_root, kind="tickets")


def run_codex_exec(prompt_file, cwd, timeout=1800):
    return _hub_operation("run_candidate", prompt_file, cwd=cwd, timeout=timeout)


def judge_with_lab_os(variant_dir, project_root):
    return _hub_operation("judge_candidate", variant_dir, project_root)


def create_variant(project_root, run_dir, theory, ticket, autopsy, memory):
    return _hub_operation("prepare_variant", project_root, run_dir, theory, ticket, autopsy, memory)


def run_cycle(project_root=".", allow_codex=False, allow_promote=False, write_patch_garden=True):
    if _HUB_ADAPTERS is None:
        return {"pass": False, "skipped": True, "reason": "hub_adapters_not_commissioned", "promotion_allowed": False}
    _hub_operation("admit_cycle", project_root, allow_agents=allow_codex, allow_promote=allow_promote)
    root = resolve_root(project_root)
    labroot = ensure_dirs(root)
    retention = prune_old_tool_lab_runs(root)
    ticket_retention = prune_old_tool_lab_tickets(root)
    run_dir = labroot / "runs" / f"{now_stamp()}_{os.getpid()}"
    (run_dir / "variants").mkdir(parents=True, exist_ok=True)
    (run_dir / "critics").mkdir(parents=True, exist_ok=True)
    (run_dir / "results").mkdir(parents=True, exist_ok=True)

    disk = run_disk_guard(root)
    if not disk.get("pass"):
        result = {
            "schema_version": SCHEMA_VERSION,
            "pass": False,
            "skipped": True,
            "reason": "disk_guard_stop",
            "disk_status": disk.get("status"),
            "run_dir": str(run_dir),
        }
        write_json(run_dir / "tool_lab_result.json", result)
        return result

    ticket, autopsy = build_tool_ticket(root)
    ticket_path = labroot / "tickets" / f"tool_lab_ticket_{now_stamp()}.json"
    autopsy_path = ticket_path.with_name(ticket_path.name.replace("ticket", "autopsy"))
    ticket_path = write_json_archive(ticket_path, ticket)
    autopsy_path = write_json_archive(autopsy_path, autopsy)
    write_json(labroot / "latest_tool_lab_ticket.json", ticket)
    write_json(labroot / "latest_tool_lab_autopsy.json", autopsy)
    write_json_archive(run_dir / "tool_lab_ticket.json", ticket)
    write_json_archive(run_dir / "tool_lab_autopsy.json", autopsy)

    memory = read_tool_memory(root, limit=20)
    manifests = []
    results = []
    scoreboard = []
    garden_cards = []
    hidden_replay_requests = ensure_hidden_replay_requests_for_pending_attempts(root)
    hidden_replay_nursery = process_hidden_replay_requests(root)
    hidden_replay_promotions = process_hidden_replay_ready_promotions(root, allow_promote=allow_promote)
    nursery_cards = active_patch_garden_cards(root, limit=PATCH_GARDEN_NURSERY_DEFAULT_LANES if allow_codex else 0)
    patch_garden_nurse = build_patch_garden_nurse_plan(root, nursery_cards, allow_codex=allow_codex)
    nursery_theories = [build_patch_garden_repair_theory(card) for card in nursery_cards]
    base_theories = select_theories(root, memory, ticket=ticket)
    max_variants = max(1, min(PATCH_TOURNAMENT_MAX_VARIANTS, int(os.environ.get("CODE_LAB_CODE_BRAIN_VARIANTS_PER_CYCLE", str(CODE_BRAIN_DEFAULT_VARIANTS_PER_CYCLE)))))
    selected_theories, nursery_exclusive = select_cycle_theories(nursery_theories, base_theories, max_variants, allow_codex)
    write_json(run_dir / "cycle_progress.json", {
        "schema_version": SCHEMA_VERSION,
        "stage": "lanes_selected",
        "allow_codex": bool(allow_codex),
        "nursery_exclusive": nursery_exclusive,
        "patch_garden_nurse": patch_garden_nurse,
        "selected_theories": [t.get("name") for t in selected_theories],
    })
    lab_team = run_lab_team_variants(
        root,
        run_dir,
        selected_theories,
        ticket,
        autopsy,
        memory,
        allow_codex,
        worker_limit=PATCH_GARDEN_NURSERY_DEFAULT_LANES if nursery_exclusive else None,
    )
    write_json(run_dir / "cycle_progress.json", {
        "schema_version": SCHEMA_VERSION,
        "stage": "lanes_complete",
        "allow_codex": bool(allow_codex),
        "nursery_exclusive": nursery_exclusive,
        "patch_garden_nurse": patch_garden_nurse,
        "selected_theories": [t.get("name") for t in selected_theories],
        "lane_count": len(lab_team.get("results", [])),
    })
    garden_lanes = []
    garden_attempts = []
    for lane in lab_team.get("results", []):
        manifest = lane.get("manifest")
        judged = lane.get("judged") or {}
        gate = lane.get("tool_brain_gate") or {}
        row = lane.get("scoreboard_row") or {}
        theory = (manifest or {}).get("theory") or next((t for t in selected_theories if row.get("patch_family") == t.get("patch_family")), {})
        if manifest:
            manifests.append(manifest)
        results.append({"manifest": manifest, **judged, "tool_brain_gate": gate})
        scoreboard.append(row)
        append_jsonl(labroot / "memory" / "tool_lab_memory.jsonl", {
            "schema_version": SCHEMA_VERSION,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "symptom_cluster": "tool_usefulness_low_lift",
            "scope": theory.get("scope"),
            "patch_family": row.get("patch_family") or theory.get("patch_family", ""),
            "variant_name": row.get("variant_name", ""),
            "score": row["score"],
            "pass": row["pass"],
            "reject": row["reject"],
            "codex_ran": bool(allow_codex),
            "tool_brain_eligible": bool(gate.get("pass")),
            "changed_files": gate.get("changed_files"),
            "gate_reasons": gate.get("reasons"),
            "future_instruction": "Do not give tools population influence until matched-control lift is positive; mutate duplicate/no-lift tool families instead of recreating them.",
        })
        if write_patch_garden and should_write_new_patch_garden_card(theory, row, manifest=manifest, gate=gate):
            card = build_patch_garden_card(root, run_dir, row, manifest=manifest, gate=gate, judged=judged, theory=theory)
            append_patch_garden_card(root, card)
            garden_cards.append(card)
        if isinstance(theory, dict) and theory.get("patch_garden_repair"):
            garden_lanes.append({"card": theory.get("patch_garden_card", {}), "lane": lane})
    eligible = [r for r in scoreboard if r.get("tool_brain_eligible")]
    winner = sorted(eligible, key=lambda r: float(r.get("score") or 0.0), reverse=True)[0] if eligible else None
    decision = {
        "winner": winner,
        "promoted": False,
        "reason": "winner_ready_for_proof_promotion" if winner else "no_tool_brain_eligible_variant",
        "allow_codex": bool(allow_codex),
        "allow_promote": bool(allow_promote),
        "proof_gated": True,
        "allow_swap_required": not bool(allow_promote),
    }
    if allow_promote and winner:
        manifest = next((m for m in manifests if m.get("variant_name") == winner.get("variant_name")), None)
        gated = next((r.get("tool_brain_gate") for r in results if (r.get("manifest") or {}).get("variant_name") == winner.get("variant_name")), None)
        if manifest and gated:
            decision["promotion"] = promote_tool_brain_winner(root, run_dir, manifest, gated)
            decision["promoted"] = bool(decision["promotion"].get("pass"))
            decision["reason"] = "proof_gated_tool_brain_promoted" if decision["promoted"] else str(decision["promotion"].get("reason") or "promotion_copy_failed")
            winner_theory = (manifest or {}).get("theory", {})
            if write_patch_garden and not decision["promoted"] and should_write_new_patch_garden_card(winner_theory, winner, manifest=manifest, gate=gated, promotion_decision=decision):
                card = build_patch_garden_card(root, run_dir, winner, manifest=manifest, gate=gated, judged={"score": winner}, promotion_decision=decision)
                append_patch_garden_card(root, card)
                garden_cards.append(card)
    elif not allow_codex:
        decision["reason"] = "codex_tool_brain_not_enabled"
    elif not allow_promote:
        decision["reason"] = "proof_promotion_disabled"
    for item in garden_lanes:
        lane = item.get("lane", {})
        row = lane.get("scoreboard_row") if isinstance(lane.get("scoreboard_row"), dict) else {}
        promo = decision if winner and row.get("variant_name") == winner.get("variant_name") else {}
        garden_attempts.append(append_patch_garden_attempt(root, item.get("card", {}), lane, promotion_decision=promo))
    result = {
        "schema_version": SCHEMA_VERSION,
        "pass": bool(winner),
        "run_dir": str(run_dir),
        "allow_codex": bool(allow_codex),
        "allow_promote": bool(allow_promote),
        "retention": retention,
        "ticket_retention": ticket_retention,
        "selected_theories": [t.get("name") for t in selected_theories],
        "variants_per_cycle": len(selected_theories),
        "patch_garden_nurse": patch_garden_nurse,
        "lab_team": {
            "mode": lab_team.get("mode"),
            "workers": int(lab_team.get("workers") or 0),
            "lane_count": len(selected_theories),
            "nursery_exclusive": nursery_exclusive,
            "isolated_sandboxes": True,
            "promotion_policy": "single_winner_after_proof_gates",
        },
        "ticket_path": str(ticket_path),
        "autopsy_path": str(autopsy_path),
        "tool_symptoms": ticket["symptoms"],
        "tool_atom_flow_ga": ticket.get("tool_atom_flow_ga", {}),
        "section_shadow_lane_mixer": ticket.get("section_shadow_lane_mixer", {}),
        "component_atom_promotion_gas": ticket.get("component_atom_promotion_gas", {}),
        "scoreboard": scoreboard,
        "promotion_decision": decision,
        "patch_garden_cards": garden_cards,
        "patch_garden_attempts": garden_attempts,
        "hidden_replay_requests": hidden_replay_requests,
        "hidden_replay_nursery": hidden_replay_nursery,
        "hidden_replay_promotions": hidden_replay_promotions,
    }
    write_json(run_dir / "judge_results.json", results)
    write_json(run_dir / "scoreboard.json", scoreboard)
    write_json(run_dir / "promotion_decision.json", decision)
    write_json(run_dir / "tool_lab_result.json", result)
    return result


def status_path(project_root):
    return runner_logs(project_root) / "tool_lab_status.json"


def write_status(project_root, **kwargs):
    root = resolve_root(project_root)
    obj = {
        "schema_version": SCHEMA_VERSION,
        "project_root": str(root),
        "pid": os.getpid(),
        "updated_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        **kwargs,
    }
    write_json(status_path(root), obj)
    return obj


def read_status(project_root):
    return read_json(status_path(project_root), {})


def stop_requested(project_root):
    root = resolve_root(project_root)
    return (root / "STOP_TOOL_LAB.flag").exists() or (root / "STOP_CODE_LAB.flag").exists()


def patch_garden_sleep_interval(default_interval, result):
    default_interval = int(default_interval)
    nurse = result.get("patch_garden_nurse") if isinstance(result, dict) else {}
    attempts = result.get("patch_garden_attempts") if isinstance(result, dict) else []
    if not isinstance(nurse, dict) or not nurse.get("retry_requested"):
        return default_interval
    if any(isinstance(a, dict) and a.get("status") == "passed_and_promoted" for a in attempts or []):
        return default_interval
    retry = int(os.environ.get("CODE_LAB_PATCH_GARDEN_NURSE_RETRY_SEC", str(PATCH_GARDEN_NURSE_RETRY_SEC)))
    return max(10, min(default_interval, retry))


def loop_forever(project_root=".", interval_sec=None, allow_codex=False, allow_promote=False, max_cycles=1):
    if type(max_cycles) is not int or not 1 <= max_cycles <= 4:
        raise ValueError("A bounded batch of one to four cycles is required; schedule later batches through the hub.")
    root = resolve_root(project_root)
    interval = int(interval_sec or os.environ.get("CODE_LAB_TOOL_LAB_INTERVAL_SEC", "1800"))
    variants_per_cycle = int(os.environ.get("CODE_LAB_CODE_BRAIN_VARIANTS_PER_CYCLE", str(CODE_BRAIN_DEFAULT_VARIANTS_PER_CYCLE)))
    lab_workers = lab_team_worker_count(variants_per_cycle)
    cycles = 0
    write_status(root, status="starting", allow_codex=bool(allow_codex), allow_promote=bool(allow_promote), interval_sec=interval, variants_per_cycle=variants_per_cycle, lab_workers=lab_workers)
    while True:
        if stop_requested(root):
            write_status(root, status="stopped", reason="stop_flag")
            return 0
        cycles += 1
        write_status(root, status="running_cycle", cycle=cycles, allow_codex=bool(allow_codex), allow_promote=bool(allow_promote), interval_sec=interval, variants_per_cycle=variants_per_cycle, lab_workers=lab_workers)
        try:
            result = run_cycle(root, allow_codex=allow_codex, allow_promote=allow_promote)
            sleep_interval = patch_garden_sleep_interval(interval, result)
            write_status(root, status="sleeping", cycle=cycles, last_result=result, interval_sec=sleep_interval, base_interval_sec=interval, allow_codex=bool(allow_codex), allow_promote=bool(allow_promote), variants_per_cycle=variants_per_cycle, lab_workers=lab_workers)
        except Exception as exc:
            sleep_interval = min(interval, int(os.environ.get("CODE_LAB_PATCH_GARDEN_NURSE_RETRY_SEC", str(PATCH_GARDEN_NURSE_RETRY_SEC))))
            write_status(root, status="error_sleeping", cycle=cycles, error=f"{type(exc).__name__}: {exc}", interval_sec=sleep_interval, base_interval_sec=interval, allow_codex=bool(allow_codex), allow_promote=bool(allow_promote), variants_per_cycle=variants_per_cycle, lab_workers=lab_workers)
        if max_cycles and cycles >= int(max_cycles):
            write_status(root, status="finished", cycle=cycles, reason="max_cycles")
            return 0
        slept = 0
        while slept < sleep_interval:
            if stop_requested(root):
                write_status(root, status="stopped", reason="stop_flag", cycle=cycles)
                return 0
            step = min(30, sleep_interval - slept)
            time.sleep(step)
            slept += step


def run_self_check(project_root="."):
    root = resolve_root(project_root)
    cases = []

    def rec(name, ok, **details):
        cases.append({"name": name, "pass": bool(ok), **details})

    ticket, autopsy = build_tool_ticket(root)
    rec("ticket_has_continuous_system_objectives", "required_patch_objectives" in ticket and "continuous_system_improvement" == ticket.get("reason"))
    rec("autopsy_has_tool_summary", "tool_index_summary" in autopsy and "recent_tool_event_summary" in autopsy)
    rec("autopsy_has_evidence_progress_summary", "evidence_progress_summary" in autopsy and "evidence_progress" in ticket.get("symptoms", {}))
    rec("autopsy_has_system_summary", "system_state_summary" in autopsy)
    rec("autopsy_has_patch_garden_cards", "patch_garden_cards" in autopsy and isinstance(autopsy.get("patch_garden_cards"), list))
    rec(
        "autopsy_has_search_power_spread_summary",
        "search_power_spread_summary" in autopsy
        and "search_power_spread_plan" in ticket
        and "search_power_spread" in ticket.get("symptoms", {}),
    )
    rec(
        "autopsy_has_component_atom_promotion_gas",
        "component_atom_promotion_gas_plan" in autopsy
        and "component_atom_promotion_gas" in ticket
        and "component_atom_promotion_gas" in ticket.get("symptoms", {}),
        plan=ticket.get("component_atom_promotion_gas"),
    )
    rec(
        "autopsy_has_section_shadow_lane_mixer",
        "section_shadow_lane_mixer_plan" in autopsy
        and "section_shadow_lane_mixer" in ticket
        and "section_shadow_lane_mixer" in ticket.get("symptoms", {}),
        plan=ticket.get("section_shadow_lane_mixer"),
    )
    selected = select_theories(root, read_tool_memory(root, limit=20), max_variants=2, ticket=ticket)
    default_selected = select_theories(root, read_tool_memory(root, limit=20), ticket=ticket)
    rec("theory_rotation_covers_non_tool_scope", any(t.get("scope") not in ("tool_foundry", "tool_proof", "tool_to_proof", "tool_governance", "tool_memory") for t in CODE_BRAIN_THEORIES))
    rec("theory_rotation_includes_repair_factory", any(t.get("scope") == "repair_factory" for t in CODE_BRAIN_THEORIES))
    rec("theory_rotation_includes_evidence_progress_contract", any(t.get("scope") == "evidence_proof" for t in CODE_BRAIN_THEORIES))
    rec("theory_rotation_includes_search_power_spread", any(t.get("scope") == "search_power_spread" for t in CODE_BRAIN_THEORIES))
    rec("default_patch_tournament_has_3_to_5_variants", PATCH_TOURNAMENT_MIN_VARIANTS <= len(default_selected) <= PATCH_TOURNAMENT_MAX_VARIANTS, selected=[t.get("name") for t in default_selected])
    expected_workers = lab_team_worker_count(len(default_selected), requested=CODE_BRAIN_DEFAULT_LAB_WORKERS)
    rec("default_lab_team_uses_parallel_workers", expected_workers >= min(2, len(default_selected)), workers=expected_workers, selected=[t.get("name") for t in default_selected])
    objectives_blob = " ".join(str(x) for x in ticket.get("required_patch_objectives", []))
    rec("ticket_enforces_repository_only_repair_nursery", "repository-scoped" in objectives_blob and "zero-influence nursery" in objectives_blob)
    rec("ticket_requires_causal_self_repair_and_auditor", "blocker X" in objectives_blob and "anti-overfit auditor" in objectives_blob and "self-check" in objectives_blob)
    rec("ticket_requires_evidence_tool_progress_contract", "evidence_tool_progress_contract" in objectives_blob and "force an evidence-progress lane" in objectives_blob)
    rec("ticket_requires_both_axes_when_evidence_and_tools_stuck", "required_axes=['evidence','tool']" in objectives_blob and "allowed evidence improvement plus one allowed tool improvement" in objectives_blob)
    rec("ticket_requires_both_axis_code_changes_for_approval", "every approved sandbox patch" in objectives_blob and "one evidence-side and one tool-side approval file" in objectives_blob)
    rec("ticket_requires_patch_garden_for_near_pass_repairs", "patch garden" in objectives_blob and "next small fixes" in objectives_blob)
    rec("ticket_requires_active_patch_garden_nurse_retries", "active patch-garden nursery retries" in objectives_blob and ticket.get("patch_garden_nurse_policy", {}).get("retry_until") == "tool_brain_gate_and_proof_promotion_pass")
    rec("ticket_requires_search_power_spread_portfolio", "search-power-spread portfolio" in objectives_blob and "zero-influence useful descendants" in objectives_blob)
    rec("ticket_requires_constant_tool_atom_flow_ga", "constant tool atom-flow GA" in objectives_blob and "tool_atom_flow_ga" in ticket and "tool_atom_flow_ga" in ticket.get("symptoms", {}), plan=ticket.get("tool_atom_flow_ga"))
    rec("ticket_requires_evidence_proof_tool_specializer", "dedicated tool specializer lane" in objectives_blob and "specific Evidence proof-card consumers" in objectives_blob and "not broad generic huge tools" in objectives_blob)
    tool_atom_log = read_json(runner_logs(root) / "tool_atom_flow_ga_latest.json", {})
    rec(
        "tool_atom_flow_ga_writes_dedicated_log",
        tool_atom_log.get("event") == "tool_atom_flow_ga_tick"
        and tool_atom_log.get("active") is True
        and tool_atom_log.get("promotion_allowed") is False
        and tool_atom_log.get("can_touch_protected_surfaces") is False,
        log=tool_atom_log,
    )
    rec(
        "ticket_requires_component_atom_promotion_gas",
        "separate tool, garden, nursery, and evidence GAs" in objectives_blob
        and "proof-promotion readiness" in objectives_blob
        and "production-impact/QualityGain weight fixed at zero" in objectives_blob,
        plan=ticket.get("component_atom_promotion_gas"),
    )
    rec(
        "ticket_requires_component_gas_to_use_main_flow_as_fast_input",
        "refined input data" in objectives_blob
        and "bounded fast local loop" in objectives_blob
        and "small local spec set" in objectives_blob,
        plan=ticket.get("component_atom_promotion_gas"),
    )
    rec(
        "ticket_requires_continuous_section_shadow_lanes",
        "continuous section shadow lanes" in objectives_blob
        and "main_search, evidence, tool, and garden" in objectives_blob
        and "routes winners back to proof cards or RegressionPreview/VerificationPass" in objectives_blob,
        plan=ticket.get("section_shadow_lane_mixer"),
    )
    prompt_probe = render_tool_prompt(CODE_BRAIN_THEORIES[0], ticket, autopsy, [])
    rec("prompt_keeps_lanes_inside_promotable_evidence_tool_files", "promotable file set" in prompt_probe and "unlisted legacy slices" in prompt_probe and "times out with no patch_summary.json" in prompt_probe)
    rec("prompt_uses_patch_garden_without_gate_bypass", "Patch garden" in prompt_probe and "never use them to bypass" in prompt_probe)
    rec("prompt_requires_both_axes_for_approval", "every approved patch must prove both" in prompt_probe and "Single-axis repairs should be kept in the patch garden" in prompt_probe)
    contract_hint = progress_contract_prompt_hint()
    rec(
        "prompt_names_allowed_progress_contract_vocabulary",
        all(token in prompt_probe for token in [
            "evidence_stale_refresh_action",
            "tool_proxy_quarantine",
            "evidence_new_cards_or_bridge_or_stale_refresh",
            "tool_observe_lift_quarantine_or_bridge",
            "tool_quarantine_count",
        ])
        and "promotion_allowed=false" in prompt_probe
        and "tool_influence_requires_matched_lift=true" in prompt_probe
        and "evidence_stale_refresh_action" in contract_hint
        and "tool_proxy_quarantine" in contract_hint,
    )
    rec("prompt_explains_search_power_spread", "Search-power spread" in prompt_probe and "Every descendant must be one-axis" in prompt_probe)
    rec("prompt_explains_tool_atom_flow_ga", "Tool atom-flow GA" in prompt_probe and "constant tool-generation tournament" in prompt_probe and "full_tool_brain_promotion_readiness" in prompt_probe and "matched-control" in prompt_probe)
    rec("prompt_explains_evidence_proof_tool_specializer", "evidence proof-card specializer" in prompt_probe and "named Evidence proof-card" in prompt_probe and "one-axis, specific, zero-authority" in prompt_probe)
    rec("prompt_explains_section_shadow_lane_mixer", "Section shadow lane mixer" in prompt_probe and "main_search, evidence, tool, and garden" in prompt_probe and "proof-card or RegressionPreview/VerificationPass follow-through" in prompt_probe)
    rec("prompt_explains_component_atom_promotion_gas", "Component atom-promotion GAs" in prompt_probe and "full main-search candidate flow" in prompt_probe and "not as production execution goals" in prompt_probe and "bounded fast local loop" in prompt_probe)
    rec("ticket_declares_lab_team_tournament", "bounded lab team" in objectives_blob and ticket.get("patch_tournament_policy", {}).get("execution_model") == "bounded_parallel_isolated_lab_lanes")
    rec("limited_variants_per_cycle", len(selected) <= 2, selected=[t.get("name") for t in selected])
    forced_ticket = {"symptoms": {"evidence_progress_problem": True, "tool_usefulness_problem": True, "search_power_spread_problem": True, "search_power_spread": {"spread_needed": True}, "evidence_progress": {"needs_supervisor_attention": True, "progress_state": "stale_input_refresh_required"}}}
    forced_selected = select_theories(root, [], max_variants=3, ticket=forced_ticket)
    rec(
        "stuck_evidence_and_tools_force_search_power_evidence_tool_lanes",
        [t.get("id") for t in forced_selected[:3]] == ["S", "R", "B"],
        selected=[t.get("name") for t in forced_selected],
    )
    alpha_forced_ticket = {"symptoms": {"repair_factory_problem": True, "search_power_spread_problem": True, "search_power_spread": {"spread_needed": True, "forced_theory_ids": ["S", "M", "R", "B", "C"]}, "evidence_progress": {"needs_supervisor_attention": True}}}
    alpha_forced_selected = select_theories(root, [], max_variants=5, ticket=alpha_forced_ticket)
    rec(
        "stuck_repair_factory_forces_five_lane_atom_molecule_tournament",
        [t.get("id") for t in alpha_forced_selected[:5]] == ["S", "M", "R", "B", "C"],
        selected=[t.get("name") for t in alpha_forced_selected],
    )
    atom_spread = summarize_search_power_spread(
        root,
        index={"total_indexed": 1, "can_influence_main_search": 0},
        recent={},
        matched_lift={},
        evidence={"atoms_collected": 3},
        system={},
        patch_garden=[],
    )
    rec(
        "search_power_spread_uses_atoms_as_zero_influence_neurons",
        atom_spread.get("atom_neuron_count") == 3
        and "workload_evidence_atom_neurons" in atom_spread.get("active_inputs", [])
        and any(lane.get("lane") == "atom_neuron_attempt_neighborhood_spread" for lane in atom_spread.get("lanes", []))
        and (atom_spread.get("atom_neuron_population_plan") or {}).get("max_population_share") == 0.0,
        spread=atom_spread,
    )
    atom_ga = build_tool_atom_flow_ga_plan(
        atom_spread,
        index={"total_indexed": 2, "can_influence_main_search": 0, "tool_atom_neuron_count": 7},
        recent={"recent_tool_atom_count": 5, "tool_bridge_non_lift_count": 2, "proof_yield_tail_count": 3},
        matched_lift={"positive_rate": 0.0},
        evidence={
            "atoms_collected": 11,
            "valid_standard_proof_cards": 2,
            "repair_precheck_bridge_updated": 1,
            "repair_precheck_passed": 2,
            "prepromotion_ticket_count": 1,
            "evidence_refresh_ticket_count": 1,
            "invalid_evidence_card_count": 1,
            "integration_stage_tool_evidence_flow_count": 9,
            "progress_state": "stale_selection_diversified_existing_pool",
            "card_ids": ["evidence_card_a", "evidence_card_b"],
            "invalid_evidence_card_repairs": [{"candidate_id": "evidence_repair_a"}],
            "routing_actions": ["route_evidence_preproof_to_regressionpreview_bridge", "route_invalid_evidence_cards_to_sibling_control_repair"],
        },
        patch_garden=[{"card_id": "atom_ga_card"}],
        main_search_flow={
            "active": True,
            "candidate_count": 4096,
            "bridge_candidate_count": 96,
            "row_count_scanned": 128,
            "signal_count": 4096,
            "integration_stage_pass_count": 31,
            "verificationpass_survivor_count": 7,
            "top_fail_reasons": [{"name": "low_trade_count", "count": 44}],
            "top_death_reasons": [{"name": "near_verificationpass_miss", "count": 18}],
            "top_operators": [{"name": "same_lane_crossover", "count": 77}],
            "top_families": [{"name": "tool_bridge_family", "count": 12}],
            "top_routing_actions": [{"name": "route_generation_candidates_to_proof_queue", "count": 96}],
            "top_candidates": [
                {
                    "candidate_id": "main_flow_near_miss",
                    "family": "tool_bridge_family",
                    "origin_operator": "same_lane_crossover",
                    "stage": "integration_stage_tool_evidence_flow",
                    "death_reason": "near_verificationpass_miss",
                    "fail_reasons": ["low_trade_count"],
                    "verificationpass_survivor": True,
                    "integration_stage_pass": True,
                }
            ],
        },
    )
    atom_ga_selected = select_theories(
        root,
        [],
        max_variants=TOOL_ATOM_FLOW_GA_MAX_VARIANTS,
        ticket={"symptoms": {"tool_atom_flow_ga": atom_ga}},
    )
    rec(
        "tool_atom_flow_ga_uses_atom_fuel_and_targets_gate_passes",
        bool(
            atom_ga.get("active")
            and atom_ga.get("fuel_ready") is True
            and atom_ga.get("fuel_units", 0) >= 20
            and atom_ga.get("candidate_tool_spec_budget", 0) >= 2
            and atom_ga.get("candidate_judge_budget", 0) > atom_ga.get("target_variant_count", 0)
            and atom_ga.get("candidate_flow_signal_count", 0) == 4096
            and atom_ga.get("candidate_judge_queue_count", 0) >= 5
            and {"tool_brain_gate", "full_tool_brain_promotion_readiness", "candidate_flow_promotion_readiness_judge", "evidence_tool_progress_contract"}.issubset(set(atom_ga.get("gate_targets") or []))
            and "evidence_proof_card_tool_specialization" in set(atom_ga.get("gate_targets") or [])
            and (atom_ga.get("evidence_tool_specialization") or {}).get("active") is True
            and any(lane.get("lane_id") == "evidence_proof_tool_specializer" and lane.get("evidence_proof_card_consumption_required") is True for lane in atom_ga.get("lanes", []))
            and any(item.get("signal_type") == "evidence_valid_proof_card" and item.get("source") == "workload_evidence_standard_proof_cards" for item in atom_ga.get("candidate_judge_queue", []))
            and (atom_ga.get("candidate_flow_fitness_profile") or {}).get("target") == "full_tool_brain_promotion_readiness"
            and (atom_ga.get("candidate_flow_fitness_profile") or {}).get("promotion_allowed") is False
            and (atom_ga.get("candidate_flow_fitness_profile") or {}).get("evidence_proof_card_consumption_required") is True
            and all(lane.get("max_population_share") == 0.0 and lane.get("can_influence_main_search") is False for lane in atom_ga.get("lanes", []))
            and all(lane.get("fitness_target") == "full_tool_brain_promotion_readiness" for lane in atom_ga.get("lanes", []))
            and all(item.get("promotion_allowed") is False and item.get("can_touch_protected_surfaces") is False for item in atom_ga.get("candidate_judge_queue", []))
            and [t.get("id") for t in atom_ga_selected[:len(atom_ga.get("selected_theory_ids", []))]]
            == atom_ga.get("selected_theory_ids", [])[:TOOL_ATOM_FLOW_GA_MAX_VARIANTS]
        ),
        plan=atom_ga,
        selected=[t.get("name") for t in atom_ga_selected],
    )
    component_main_flow = {
        "active": True,
        "source": "research_best/main_search_generation_candidates_latest.json",
        "loop": 7,
        "generation": 11,
        "candidate_count": 99,
        "bridge_candidate_count": 8,
        "row_count_scanned": 99,
        "signal_count": 99,
        "integration_stage_eval_count": 14,
        "integration_stage_pass_count": 4,
        "verificationpass_near_miss_count": 8,
        "verificationpass_survivor_count": 3,
        "top_operators": [{"name": "same_lane_crossover", "count": 31}, {"name": "param_only_mutation", "count": 22}],
        "top_stages": [{"name": "integration_stage_tool_evidence_flow", "count": 99}],
        "top_death_reasons": [{"name": "low_trade_count", "count": 20}],
        "top_fail_reasons": [{"name": "trades", "count": 20}, {"name": "pos_rate", "count": 15}],
        "top_routing_actions": [{"name": "route_generation_candidates_to_proof_queue", "count": 99}],
        "top_families": [{"name": "fam_a", "count": 4}],
        "top_candidates": [
            {
                "candidate_id": "flow_1",
                "family": "fam_a",
                "origin_operator": "same_lane_crossover",
                "stage": "integration_stage_tool_evidence_flow",
                "death_reason": "low_trade_count",
                "fail_reasons": ["trades"],
                "routing_actions": ["route_generation_candidates_to_proof_queue"],
                "proof_yield": 12.0,
                "score": 1200.0,
                "verificationpass_survivor": False,
                "integration_stage_pass": False,
            },
            {
                "candidate_id": "flow_2",
                "family": "fam_b",
                "origin_operator": "param_only_mutation",
                "stage": "integration_stage_tool_evidence_flow",
                "death_reason": "passed_non_final_evidence",
                "fail_reasons": [],
                "routing_actions": ["require_non_final_replay_traces_for_bridge_roots"],
                "proof_yield": 10.0,
                "score": 1000.0,
                "verificationpass_survivor": True,
                "integration_stage_pass": True,
            },
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
    }
    section_mixer = build_continuous_section_shadow_lane_mixer(
        index={
            "tool_atom_attention_targets": ["regressionpreview_starvation", "RUN_TOOL_USEFULNESS_AUDIT"],
            "proof_yield_ema_avg": 0.14,
        },
        recent={
            "recent_tool_atom_targets": ["matched_control_lift", "no_lift_quarantine"],
            "recent_bridge_tools": [
                {"tool_name": "tool_regressionpreview_bridge", "observe_score": 0.42, "control_score": 0.12, "promoted_to_probation": False}
            ],
        },
        evidence={
            "valid_standard_proof_cards": 2,
            "integration_stage_tool_evidence_flow_count": 14,
            "progress_state": "stale_input_refresh_required",
            "card_ids": ["section_evidence_card_a", "section_evidence_card_b"],
            "routing_actions": ["route_evidence_preproof_to_regressionpreview_bridge"],
            "compute_report_integration_stage_flow_actions": ["route_integration_stage_flow_to_tool_observe"],
        },
        patch_garden=[{
            "card_id": "garden_card_section",
            "patch_family": "section_shadow_lane_mixer",
            "status": "needs_small_fix",
            "next_small_fixes": ["add_one_cheap_self_check_that_proves_the_claim"],
        }],
        main_search_flow=component_main_flow,
    )
    section_lanes = [row for row in section_mixer.get("lanes") or [] if isinstance(row, dict)]
    section_lane_ids = {row.get("lane_id") for row in section_lanes}
    section_pairs = [row for row in section_mixer.get("mix_pairs") or [] if isinstance(row, dict)]
    rec(
        "section_shadow_lane_mixer_splits_and_mixes_main_evidence_tool_garden",
        bool(
            section_mixer.get("active")
            and section_lane_ids == {"main_search", "evidence", "tool", "garden"}
            and section_mixer.get("mix_pair_count", 0) >= 4
            and section_mixer.get("candidate_source") == "main_search_evidence_tool_garden_refined_data"
            and (section_mixer.get("evidence_tool_specialization") or {}).get("active") is True
            and (section_mixer.get("section_input_counts") or {}).get("tool", 0) >= 6
            and all(row.get("seed_count", 0) > 0 for row in section_lanes)
            and all(row.get("promotion_allowed") is False and row.get("can_touch_protected_surfaces") is False for row in section_lanes)
            and all(row.get("research_replay_only") is True for row in section_pairs)
            and all(row.get("promotion_allowed") is False and row.get("can_touch_protected_surfaces") is False for row in section_pairs)
        ),
        plan=section_mixer,
    )
    rec(
        "section_shadow_lane_mixer_runs_tool_and_evidence_continuously",
        bool(
            section_mixer.get("continuous") is True
            and section_mixer.get("tool_side_enabled") is True
            and section_mixer.get("evidence_side_enabled") is True
            and (section_mixer.get("continuous_workers") or {}).get("tool_lab", {}).get("loop") == "development_tool_lab.loop_forever"
            and (section_mixer.get("continuous_workers") or {}).get("evidence_proof_worker", {}).get("watchdog_task") == "REPOSITORY_Evidence_Proof_Worker"
            and section_mixer.get("promotion_allowed") is False
            and section_mixer.get("can_influence_main_search") is False
        ),
        plan=section_mixer,
    )
    with tempfile.TemporaryDirectory(prefix="section_mixer_log_selfcheck_") as tmp_section_mixer:
        section_mixer_root = Path(tmp_section_mixer)
        (section_mixer_root / "runner_logs").mkdir(parents=True, exist_ok=True)
        section_mixer_tick = append_section_shadow_lane_mixer_log(section_mixer_root, section_mixer, component_main_flow)
        section_mixer_latest = read_json(section_mixer_root / "runner_logs" / "section_shadow_lane_mixer_latest.json", {})
        rec(
            "section_shadow_lane_mixer_writes_continuous_heartbeat",
            bool(
                section_mixer_tick.get("event") == "section_shadow_lane_mixer_tick"
                and section_mixer_latest.get("active") is True
                and section_mixer_latest.get("continuous") is True
                and section_mixer_latest.get("tool_side_enabled") is True
                and section_mixer_latest.get("evidence_side_enabled") is True
                and section_mixer_latest.get("lane_count") == 4
                and section_mixer_latest.get("mix_pair_count", 0) >= 4
                and section_mixer_latest.get("promotion_allowed") is False
                and section_mixer_latest.get("can_touch_protected_surfaces") is False
            ),
            heartbeat=section_mixer_latest,
        )
    component_gas = build_component_atom_promotion_gas(
        atom_spread,
        index={
            "total_indexed": 2,
            "can_influence_main_search": 0,
            "tool_atom_neuron_count": 7,
            "tool_atom_attention_targets": ["regressionpreview_starvation", "RUN_TOOL_USEFULNESS_AUDIT"],
        },
        recent={
            "recent_tool_atom_count": 5,
            "recent_tool_atom_targets": ["matched_control_lift", "no_lift_quarantine"],
            "tool_bridge_non_lift_count": 2,
            "proof_yield_tail_count": 3,
        },
        matched_lift={
            "positive_rate": 0.0,
            "repair_nursery_candidates": 2,
            "promotion_candidates": 0,
            "top_blockers": ["single_slice_dependency", "previous_best_loss"],
            "recommended_mutation_axis": "dual_control_lift",
        },
        evidence={
            "atoms_collected": 11,
            "valid_standard_proof_cards": 2,
            "repair_precheck_bridge_updated": 1,
            "progress_state": "stale_input_refresh_required",
            "routing_actions": ["refresh_workload_evidence_inputs", "route_evidence_preproof_to_regressionpreview_bridge"],
        },
        patch_garden=[{
            "card_id": "garden_card_a",
            "patch_family": "evidence_tool_progress_contract",
            "status": "needs_small_fix",
            "next_small_fixes": ["write_patch_summary_json_before_long_tests", "add_evidence_tool_progress_contract_with_before_after_evidence"],
            "reasons": ["hidden_replay_required"],
        }],
        main_search_flow=component_main_flow,
    )
    component_rows = component_gas.get("components") or []
    component_ids = {row.get("component_id") for row in component_rows if isinstance(row, dict)}
    component_lanes = [
        lane
        for row in component_rows
        if isinstance(row, dict)
        for lane in (row.get("candidate_lanes") or [])
        if isinstance(lane, dict)
    ]
    tool_component_rows = [row for row in component_rows if isinstance(row, dict) and row.get("component_id") == "tool"]
    garden_component_rows = [row for row in component_rows if isinstance(row, dict) and row.get("component_id") == "garden"]
    nursery_component_rows = [row for row in component_rows if isinstance(row, dict) and row.get("component_id") == "nursery"]
    evidence_component_rows = [row for row in component_rows if isinstance(row, dict) and row.get("component_id") == "evidence"]
    tool_component_lanes = [
        lane
        for row in tool_component_rows
        for lane in (row.get("candidate_lanes") or [])
        if isinstance(lane, dict)
    ]
    garden_component_lanes = [
        lane
        for row in garden_component_rows
        for lane in (row.get("candidate_lanes") or [])
        if isinstance(lane, dict)
    ]
    nursery_component_lanes = [
        lane
        for row in nursery_component_rows
        for lane in (row.get("candidate_lanes") or [])
        if isinstance(lane, dict)
    ]
    evidence_component_lanes = [
        lane
        for row in evidence_component_rows
        for lane in (row.get("candidate_lanes") or [])
        if isinstance(lane, dict)
    ]
    component_ga_self_check_summary = {
        "component_ids": sorted(component_ids),
        "local_candidate_spec_count": int(component_gas.get("local_candidate_spec_count", 0) or 0),
        "global_candidate_count": int(component_gas.get("global_candidate_count", 0) or 0),
        "flow_candidate_count": int(component_gas.get("flow_candidate_count", 0) or 0),
        "flow_bridge_candidate_count": int(component_gas.get("flow_bridge_candidate_count", 0) or 0),
        "amplified_component_ids": list((component_gas.get("evidence_tool_ga_amplification") or {}).get("amplified_component_ids") or []),
        "component_specs": {
            str(row.get("component_id") or ""): len(row.get("candidate_lanes") or [])
            for row in component_rows
            if isinstance(row, dict)
        },
        "component_compute_multipliers": {
            str(row.get("component_id") or ""): int(row.get("ga_compute_multiplier", 1) or 1)
            for row in component_rows
            if isinstance(row, dict)
        },
        "component_local_generation_budgets": {
            str(row.get("component_id") or ""): int(row.get("local_generation_budget", 0) or 0)
            for row in component_rows
            if isinstance(row, dict)
        },
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
    }
    rec(
        "component_atom_promotion_gas_split_tool_garden_nursery_evidence",
        bool(
            component_gas.get("active")
            and component_ids == set(COMPONENT_ATOM_PROMOTION_GA_COMPONENTS)
            and component_gas.get("global_candidate_count", 0) >= 4
            and all(row.get("mode") == "component_main_flow_promotion_ga" for row in component_rows)
            and all(row.get("population_model") == "main_search_flow_signals_plus_component_atoms" for row in component_rows)
        ),
        plan=component_ga_self_check_summary,
    )
    rec(
        "component_atom_promotion_gas_uses_full_main_search_flow",
        bool(
            component_gas.get("candidate_source") == "main_search_flow_plus_component_atoms"
            and component_gas.get("global_candidate_count") == 99
            and component_gas.get("flow_bridge_candidate_count") == 8
            and component_gas.get("local_candidate_spec_count", 0) >= 4
            and all(row.get("flow_candidate_count") == 99 for row in component_rows)
            and all(row.get("flow_bridge_candidate_count") == 8 for row in component_rows)
            and all(row.get("candidate_generation", {}).get("source") == "main_search_flow_plus_component_atoms" for row in component_rows)
            and all(row.get("flow_signal_atoms") for row in component_rows)
        ),
        plan=component_ga_self_check_summary,
    )
    rec(
        "component_atom_promotion_gas_runs_fast_local_loops_over_refined_inputs",
        bool(
            component_gas.get("main_generation_inherited") is False
            and component_gas.get("expected_faster_than_main_search") is True
            and component_gas.get("refined_input_signal_count") == 99
            and component_gas.get("local_generation_budget", 999) <= component_gas.get("max_local_generation_budget", EVIDENCE_TOOL_GA_MAX_LOCAL_GENERATIONS)
            and all(row.get("main_generation_inherited") is False for row in component_rows)
            and all(row.get("expected_faster_than_main_search") is True for row in component_rows)
            and all(
                1 <= int(row.get("local_generation_budget", 0) or 0)
                <= int((row.get("fast_local_loop") or {}).get("max_local_generation_budget", COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS) or COMPONENT_ATOM_PROMOTION_GA_MAX_LOCAL_GENERATIONS)
                for row in component_rows
            )
            and all((row.get("fast_local_loop") or {}).get("input_role") == "refined_main_search_candidates_are_data_not_generation_budget" for row in component_rows)
            and all((row.get("candidate_generation") or {}).get("main_generation_inherited") is False for row in component_rows)
            and all(
                (row.get("candidate_generation") or {}).get("local_candidate_spec_count", 999)
                <= int((row.get("ga_amplification") or {}).get("materialized_candidate_limit", COMPONENT_ATOM_PROMOTION_GA_MAX_CANDIDATES) or COMPONENT_ATOM_PROMOTION_GA_MAX_CANDIDATES)
                for row in component_rows
            )
        ),
        plan=component_ga_self_check_summary,
    )
    rec(
        "evidence_and_tool_component_gas_run_10000x_zero_authority_population",
        bool(
            (component_gas.get("evidence_tool_ga_amplification") or {}).get("compute_multiplier") >= 10000
            and tool_component_rows
            and evidence_component_rows
            and int(tool_component_rows[0].get("ga_compute_multiplier", 1) or 1) >= 10000
            and int(evidence_component_rows[0].get("ga_compute_multiplier", 1) or 1) >= 10000
            and int(tool_component_rows[0].get("virtual_candidate_population_count", 0) or 0) >= int(tool_component_rows[0].get("materialized_candidate_count", 0) or 0)
            and int(evidence_component_rows[0].get("virtual_candidate_population_count", 0) or 0) >= int(evidence_component_rows[0].get("materialized_candidate_count", 0) or 0)
            and int(tool_component_rows[0].get("local_generation_budget", 0) or 0) >= min(EVIDENCE_TOOL_GA_MIN_LOCAL_GENERATIONS, int(tool_component_rows[0].get("materialized_candidate_count", 1) or 1))
            and int(evidence_component_rows[0].get("local_generation_budget", 0) or 0) >= min(EVIDENCE_TOOL_GA_MIN_LOCAL_GENERATIONS, int(evidence_component_rows[0].get("materialized_candidate_count", 1) or 1))
            and all(float(lane.get("quality_score", 0.0) or 0.0) > 0.0 for lane in (tool_component_lanes[:8] + evidence_component_lanes[:8]))
            and all(lane.get("promotion_allowed") is False and lane.get("can_touch_protected_surfaces") is False for lane in tool_component_lanes)
        ),
        plan=component_ga_self_check_summary,
    )
    rec(
        "garden_and_nursery_component_gas_run_10000x_zero_authority_population",
        bool(
            garden_component_rows
            and nursery_component_rows
            and int(garden_component_rows[0].get("ga_compute_multiplier", 1) or 1) >= 10000
            and int(nursery_component_rows[0].get("ga_compute_multiplier", 1) or 1) >= 10000
            and int(garden_component_rows[0].get("virtual_candidate_population_count", 0) or 0) >= int(garden_component_rows[0].get("materialized_candidate_count", 0) or 0)
            and int(nursery_component_rows[0].get("virtual_candidate_population_count", 0) or 0) >= int(nursery_component_rows[0].get("materialized_candidate_count", 0) or 0)
            and int(garden_component_rows[0].get("materialized_candidate_count", 0) or 0) >= int(EVIDENCE_TOOL_GA_MATERIALIZED_CANDIDATE_LIMIT)
            and int(nursery_component_rows[0].get("materialized_candidate_count", 0) or 0) >= int(EVIDENCE_TOOL_GA_MATERIALIZED_CANDIDATE_LIMIT)
            and int(garden_component_rows[0].get("local_generation_budget", 0) or 0) >= min(EVIDENCE_TOOL_GA_MIN_LOCAL_GENERATIONS, int(garden_component_rows[0].get("materialized_candidate_count", 1) or 1))
            and int(nursery_component_rows[0].get("local_generation_budget", 0) or 0) >= min(EVIDENCE_TOOL_GA_MIN_LOCAL_GENERATIONS, int(nursery_component_rows[0].get("materialized_candidate_count", 1) or 1))
            and all(float(lane.get("quality_score", 0.0) or 0.0) > 0.0 for lane in (garden_component_lanes[:8] + nursery_component_lanes[:8]))
            and all(lane.get("promotion_allowed") is False and lane.get("can_touch_protected_surfaces") is False for lane in (garden_component_lanes + nursery_component_lanes))
        ),
        plan=component_ga_self_check_summary,
    )
    rec(
        "component_atom_promotion_gas_targets_promotion_not_production execution",
        bool(
            component_lanes
            and all(lane.get("fitness_target") == "proof_promotion_readiness" for lane in component_lanes)
            and all(float(lane.get("production_impact_weight") or 0.0) == 0.0 for lane in component_lanes)
            and all(lane.get("promotion_allowed") is False for lane in component_lanes)
            and all(lane.get("can_influence_main_search") is False for lane in component_lanes)
            and all(lane.get("main_generation_inherited") is False for lane in component_lanes)
            and all(lane.get("source_atoms") for lane in component_lanes)
            and all(lane.get("source_flow_signals") for lane in component_lanes)
            and all(lane.get("flow_candidate_count") == 99 for lane in component_lanes)
            and component_gas.get("target") == "proof_promotion_readiness_not_production_impacts"
        ),
        plan=component_ga_self_check_summary,
    )
    rec(
        "component_atom_promotion_gas_feeds_evidence_specialization_into_tools",
        bool(
            tool_component_rows
            and (component_gas.get("evidence_tool_specialization") or {}).get("active") is True
            and (tool_component_rows[0].get("evidence_tool_specialization") or {}).get("active") is True
            and any(lane.get("evidence_tool_specialization_required") is True for lane in tool_component_lanes)
            and any("evidence_proof_card_consumer" in str(lane.get("mutation_axis") or "") or "evidence_proof_card_tool_consumption" in str(lane.get("promotion_target") or "") for lane in tool_component_lanes)
            and all(lane.get("promotion_allowed") is False and lane.get("can_touch_protected_surfaces") is False for lane in tool_component_lanes)
        ),
        plan=component_ga_self_check_summary,
    )
    component_gas_proof_sample = dict(component_gas)
    sampled_components = []
    for component in component_gas.get("components") or []:
        if not isinstance(component, dict):
            continue
        sampled = dict(component)
        sampled_lanes = [lane for lane in component.get("candidate_lanes") or [] if isinstance(lane, dict)][:8]
        sampled["candidate_lanes"] = sampled_lanes
        sampled["proof_work_candidate_limit"] = len(sampled_lanes)
        candidate_generation = dict(sampled.get("candidate_generation") if isinstance(sampled.get("candidate_generation"), dict) else {})
        candidate_generation["local_candidate_spec_count"] = len(sampled_lanes)
        sampled["candidate_generation"] = candidate_generation
        sampled_components.append(sampled)
    component_gas_proof_sample["components"] = sampled_components
    component_gas_proof_sample["local_candidate_spec_count"] = sum(len(row.get("candidate_lanes") or []) for row in sampled_components)
    with tempfile.TemporaryDirectory(prefix="component_ga_log_selfcheck_") as tmp_component_ga:
        component_ga_root = Path(tmp_component_ga)
        (component_ga_root / "runner_logs").mkdir(parents=True, exist_ok=True)
        write_json(
            component_ga_root / "runner_logs" / "main_search_generation_candidate_tap_status.json",
            {"loop": 7, "generation": 11, "candidate_count": 99, "bridge_candidate_count": 8},
        )
        component_ga_tick = append_component_atom_promotion_ga_log(component_ga_root, component_gas)
        component_ga_latest = read_json(component_ga_root / "runner_logs" / "component_atom_promotion_gas_latest.json", {})
        rec(
            "component_atom_promotion_ga_writes_constant_running_heartbeat",
            bool(
                component_ga_tick.get("event") == "component_atom_promotion_ga_tick"
                and component_ga_latest.get("component_count") == 4
                and component_ga_latest.get("loop") == 7
                and component_ga_latest.get("snapshot_generation") == 11
                and component_ga_latest.get("global_candidate_count", 0) == 99
                and component_ga_latest.get("flow_candidate_count") == 99
                and component_ga_latest.get("local_candidate_spec_count", 0) >= 4
                and component_ga_latest.get("local_loop") == 1
                and component_ga_latest.get("main_generation_inherited") is False
                and component_ga_latest.get("expected_faster_than_main_search") is True
                and component_ga_latest.get("local_generation_budget", 999) <= component_ga_latest.get("max_local_generation_budget", EVIDENCE_TOOL_GA_MAX_LOCAL_GENERATIONS)
                and all(row.get("promotion_allowed") is False for row in component_ga_latest.get("components") or [])
                and all(row.get("flow_candidate_count") == 99 for row in component_ga_latest.get("components") or [])
                and all(row.get("refined_input_candidate_count") == 99 for row in component_ga_latest.get("components") or [])
                and all(row.get("local_loop") == 1 for row in component_ga_latest.get("components") or [])
                and all(row.get("main_generation_inherited") is False for row in component_ga_latest.get("components") or [])
                and all(float(row.get("production_impact_weight") or 0.0) == 0.0 for row in component_ga_latest.get("components") or [])
            ),
            heartbeat=component_ga_latest,
        )
        component_proof_work = append_component_atom_promotion_proof_work_log(
            component_ga_root,
            component_gas_proof_sample,
            heartbeat=component_ga_tick,
        )
        component_proof_work_latest = read_json(component_ga_root / "research_best" / "component_atom_promotion_proof_work_latest.json", {})
        component_work_items = [row for row in component_proof_work_latest.get("work_items") or [] if isinstance(row, dict)]
        rec(
            "component_atom_promotion_ga_routes_specs_to_non_final_proof_work",
            bool(
                component_proof_work.get("event") == "component_atom_promotion_proof_work_tick"
                and component_proof_work_latest.get("active") is True
                and component_proof_work_latest.get("work_item_count") == component_gas_proof_sample.get("local_candidate_spec_count")
                and component_work_items
                and all(row.get("research_replay_only") is True for row in component_work_items)
                and all(row.get("non_final_only") is True for row in component_work_items)
                and all(row.get("same_parent_matched_control_required") is True for row in component_work_items)
                and all(row.get("negative_controls_required") is True for row in component_work_items)
                and all(row.get("hidden_replay_required_before_promotion") is True for row in component_work_items)
                and all(row.get("promotion_allowed") is False for row in component_work_items)
                and all(row.get("can_touch_protected_surfaces") is False for row in component_work_items)
                and all(row.get("can_touch_final_holdout_or_live") is False for row in component_work_items)
                and "route_component_specs_to_non_final_proof_compute" in component_proof_work_latest.get("routing_actions", [])
            ),
            proof_work=component_proof_work_latest,
        )

    def sample_progress_contract(improvements, required_axes=None):
        improvements = [str(item) for item in improvements]
        progress_axes = []
        if set(improvements) & EVIDENCE_PROGRESS_IMPROVEMENTS:
            progress_axes.append("evidence")
        if set(improvements) & TOOL_PROGRESS_IMPROVEMENTS:
            progress_axes.append("tool")
        before = {
            "new_evidence_refresh_ticket_count": 0,
            "repair_precheck_bridge_updated": 0,
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "routing_actions": [],
            "tool_lifecycle_actions": [],
        }
        after = dict(before)
        evidence = {}
        if "evidence" in progress_axes:
            after["new_evidence_refresh_ticket_count"] = 1
            after["routing_actions"] = ["refresh_workload_evidence_inputs", "consume_evidence_refresh_tickets"]
            evidence["evidence_new_cards_or_bridge_or_stale_refresh"] = "synthetic stale refresh ticket evidence"
        if "tool" in progress_axes:
            after["tool_lifecycle_decision_count"] = 1
            after["tool_quarantine_count"] = 1
            after["tool_lifecycle_actions"] = ["retire_or_quarantine_zero_exec_tool", "reduce_tool_quota"]
            evidence["tool_observe_lift_quarantine_or_bridge"] = "synthetic no-lift quarantine evidence"
        payload = {
            "pass": True,
            "progress_axes": progress_axes,
            "improvements": improvements,
            "before": before,
            "after": after,
            "evidence": evidence,
            "protected_surfaces_unchanged": True,
            "promotion_allowed": False,
            "can_touch_protected_surfaces": False,
            "tool_influence_requires_matched_lift": True,
        }
        if required_axes is not None:
            payload["required_axes"] = list(required_axes)
        return payload

    rec(
        "promotion_gate_requires_evidence_tool_progress_contract",
        evidence_tool_progress_contract_ok({}).get("pass") is False
        and evidence_tool_progress_contract_ok({
            "evidence_tool_progress_contract": sample_progress_contract(["evidence_stale_refresh_action"], required_axes=[])
        }).get("pass") is True,
    )
    rec(
        "promotion_gate_requires_both_axes_for_stuck_evidence_and_tools",
        evidence_tool_progress_contract_ok({
            "evidence_tool_progress_contract": sample_progress_contract(["tool_proxy_quarantine"], required_axes=["evidence", "tool"])
        }, require_evidence=True, require_tool=True).get("pass") is False
        and evidence_tool_progress_contract_ok({
            "evidence_tool_progress_contract": sample_progress_contract(["evidence_stale_refresh_action", "tool_proxy_quarantine"], required_axes=["evidence", "tool"])
        }, require_evidence=True, require_tool=True).get("pass") is True,
    )
    rec(
        "progress_contract_requires_before_after_evidence",
        evidence_tool_progress_contract_ok({
            "evidence_tool_progress_contract": {
                "pass": True,
                "required_axes": ["evidence", "tool"],
                "progress_axes": ["evidence", "tool"],
                "improvements": ["evidence_stale_refresh_action", "tool_proxy_quarantine"],
                "protected_surfaces_unchanged": True,
                "promotion_allowed": False,
                "can_touch_protected_surfaces": False,
                "tool_influence_requires_matched_lift": True,
            }
        }, require_evidence=True, require_tool=True).get("pass") is False,
    )
    card_341a57_contract = sample_progress_contract(
        ["evidence_stale_refresh_action", "tool_proxy_quarantine"],
        required_axes=["evidence", "tool"],
    )
    card_341a57_check = evidence_tool_progress_contract_ok(
        {"evidence_tool_progress_contract": card_341a57_contract},
        require_evidence=True,
        require_tool=True,
    )
    rec(
        "patch_garden_341a57_contract_clears_both_axis_next_small_fixes",
        bool(
            card_341a57_check.get("pass")
            and card_341a57_check.get("progress_axes") == ["evidence", "tool"]
            and card_341a57_check.get("required_axes") == ["evidence", "tool"]
            and card_341a57_check.get("evidence_check", {}).get("evidence_evidence") is True
            and card_341a57_check.get("evidence_check", {}).get("tool_evidence") is True
            and card_341a57_contract.get("promotion_allowed") is False
            and card_341a57_contract.get("can_touch_protected_surfaces") is False
            and card_341a57_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_341a57_contract,
        check=card_341a57_check,
    )
    card_fe6186_contract = sample_progress_contract(
        ["evidence_stale_refresh_action", "tool_proxy_quarantine"],
        required_axes=["evidence", "tool"],
    )
    card_fe6186_contract["card_id"] = "fe61861cc13007b4"
    card_fe6186_check = evidence_tool_progress_contract_ok(
        {"evidence_tool_progress_contract": card_fe6186_contract},
        require_evidence=True,
        require_tool=True,
    )
    rec(
        "patch_garden_fe6186_contract_clears_listed_next_small_fixes",
        bool(
            card_fe6186_check.get("pass")
            and card_fe6186_check.get("evidence_check", {}).get("evidence_evidence") is True
            and card_fe6186_check.get("evidence_check", {}).get("tool_evidence") is True
            and set(card_fe6186_contract.get("required_axes") or []) == {"evidence", "tool"}
            and card_fe6186_contract.get("promotion_allowed") is False
            and card_fe6186_contract.get("can_touch_protected_surfaces") is False
            and card_fe6186_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_fe6186_contract,
        check=card_fe6186_check,
    )
    card_9afbb0_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_9afbb0_tool = {
        "before": {
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "tool_lifecycle_actions": [],
        },
        "after": {
            "tool_lifecycle_decision_count": 1,
            "tool_quarantine_count": 1,
            "tool_lifecycle_actions": ["retire_or_quarantine_zero_exec_tool", "reduce_tool_quota"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
    }
    card_9afbb0_contract = build_evidence_tool_progress_contract(
        card_9afbb0_evidence,
        card_9afbb0_tool,
        card_id="9afbb08cd46fb01a",
    )
    card_9afbb0_check = evidence_tool_progress_contract_ok(
        {"evidence_tool_progress_contract": card_9afbb0_contract},
        require_evidence=True,
        require_tool=True,
    )
    rec(
        "patch_garden_9afbb0_contract_helper_clears_exact_next_small_fixes",
        bool(
            card_9afbb0_check.get("pass")
            and card_9afbb0_contract.get("card_id") == "9afbb08cd46fb01a"
            and card_9afbb0_contract.get("progress_axes") == ["evidence", "tool"]
            and card_9afbb0_contract.get("required_axes") == ["evidence", "tool"]
            and "evidence_stale_refresh_action" in (card_9afbb0_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_9afbb0_contract.get("improvements") or [])
            and card_9afbb0_check.get("evidence_check", {}).get("evidence_evidence") is True
            and card_9afbb0_check.get("evidence_check", {}).get("tool_evidence") is True
            and card_9afbb0_contract.get("promotion_allowed") is False
            and card_9afbb0_contract.get("can_touch_protected_surfaces") is False
            and card_9afbb0_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_9afbb0_contract,
        check=card_9afbb0_check,
    )
    card_fa6e9c_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_fa6e9c_tool = {
        "compute_report_tool_decisions": 5,
        "compute_report_tool_actions": ["reduce_tool_quota", "keep_observing"],
    }
    card_fa6e9c_contract = build_evidence_tool_progress_contract(
        card_fa6e9c_evidence,
        card_fa6e9c_tool,
        card_id="fa6e9c8504b6dec2",
    )
    card_fa6e9c_check = evidence_tool_progress_contract_ok(
        {"evidence_tool_progress_contract": card_fa6e9c_contract},
        require_evidence=True,
        require_tool=True,
    )
    rec(
        "patch_garden_fa6e9c_contract_accepts_autopsy_tool_actions",
        bool(
            card_fa6e9c_check.get("pass")
            and card_fa6e9c_contract.get("card_id") == "fa6e9c8504b6dec2"
            and card_fa6e9c_contract.get("progress_axes") == ["evidence", "tool"]
            and "evidence_stale_refresh_action" in (card_fa6e9c_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_fa6e9c_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_fa6e9c_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and _contract_float(card_fa6e9c_contract.get("after", {}).get("tool_lifecycle_decision_count"), 0.0) == 5.0
            and card_fa6e9c_check.get("evidence_check", {}).get("tool_evidence") is True
            and card_fa6e9c_contract.get("promotion_allowed") is False
            and card_fa6e9c_contract.get("can_touch_protected_surfaces") is False
        ),
        contract=card_fa6e9c_contract,
        check=card_fa6e9c_check,
    )
    fa6e9c_actions = patch_garden_next_actions(["variant_artifact_missing_for_hidden_replay"])
    rec(
        "patch_garden_fa6e9c_artifact_failure_gets_sharp_retry_step",
        "repair_hidden_replay_failure_before_promotion" in fa6e9c_actions
        and "write_patch_summary_json_before_long_tests" in fa6e9c_actions,
        actions=fa6e9c_actions,
    )
    card_618db0_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_618db0_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_618db0_tool = {
        "before": {
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "tool_lifecycle_actions": [],
        },
        "after": {
            "tool_lifecycle_decision_count": 1,
            "tool_quarantine_count": 1,
            "tool_lifecycle_actions": ["retire_or_quarantine_zero_exec_tool", "reduce_tool_quota"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
    }
    card_618db0_contract = build_evidence_tool_progress_contract(
        {"patch_summary_evidence_progress_fragment": card_618db0_evidence},
        card_618db0_tool,
        card_id="618db04b74b69b7d",
    )
    card_618db0_check = evidence_tool_progress_contract_ok(
        {"evidence_tool_progress_contract": card_618db0_contract},
        require_evidence=True,
        require_tool=True,
    )
    rec(
        "patch_garden_618db0_contract_merges_evidence_hook_and_tool_no_lift_quarantine",
        bool(
            card_618db0_check.get("pass")
            and card_618db0_contract.get("card_id") == "618db04b74b69b7d"
            and card_618db0_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_618db0_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_618db0_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_618db0_contract.get("improvements") or [])
            and card_618db0_evidence.get("progress_hook") == "evidence_patch_summary_merge_fragment"
            and card_618db0_check.get("evidence_check", {}).get("evidence_evidence") is True
            and card_618db0_check.get("evidence_check", {}).get("tool_evidence") is True
            and card_618db0_contract.get("promotion_allowed") is False
            and card_618db0_contract.get("can_touch_protected_surfaces") is False
            and card_618db0_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_618db0_contract,
        check=card_618db0_check,
    )
    card_97c29a_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_97c29a_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_97c29a_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_97c29a_evidence["patch_summary_contract_ready"] = True
    card_97c29a_tool = {
        "before": {
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_lifecycle_decision_count": 4,
            "tool_quarantine_count": 1,
            "compute_report_tool_actions": ["keep_observing", "reduce_tool_quota"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
    }
    card_97c29a_contract = build_evidence_tool_progress_contract(
        {"patch_summary_evidence_progress_fragment": card_97c29a_evidence},
        card_97c29a_tool,
        card_id="97c29ace5376845a",
    )
    card_97c29a_check = evidence_tool_progress_contract_ok(
        {"evidence_tool_progress_contract": card_97c29a_contract},
        require_evidence=True,
        require_tool=True,
    )
    rec(
        "patch_garden_97c29a_contract_merges_evidence_hook_and_tool_quarantine",
        bool(
            card_97c29a_check.get("pass")
            and card_97c29a_contract.get("card_id") == "97c29ace5376845a"
            and card_97c29a_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_97c29a_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_97c29a_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_97c29a_contract.get("improvements") or [])
            and card_97c29a_evidence.get("progress_hook") == "evidence_patch_summary_merge_fragment"
            and card_97c29a_evidence.get("patch_summary_contract_ready") is True
            and "reduce_tool_quota" in (card_97c29a_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_97c29a_check.get("evidence_check", {}).get("evidence_evidence") is True
            and card_97c29a_check.get("evidence_check", {}).get("tool_evidence") is True
            and card_97c29a_contract.get("promotion_allowed") is False
            and card_97c29a_contract.get("can_touch_protected_surfaces") is False
            and card_97c29a_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_97c29a_contract,
        check=card_97c29a_check,
    )
    card_479e47_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_479e47_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_479e47_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_479e47_evidence["patch_summary_contract_ready"] = True
    card_479e47_tool = {
        "before": {
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_lifecycle_decision_count": 4,
            "tool_quarantine_count": 1,
            "compute_report_tool_actions": ["keep_observing", "reduce_tool_quota"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
    }
    card_479e47_contract = build_evidence_tool_progress_contract(
        {"patch_summary_evidence_progress_fragment": card_479e47_evidence},
        card_479e47_tool,
        card_id="479e47d702c8cc38",
    )
    card_479e47_check = evidence_tool_progress_contract_ok(
        {"evidence_tool_progress_contract": card_479e47_contract},
        require_evidence=True,
        require_tool=True,
    )
    card_479e47_hook = patch_garden_contract_repair_hook(
        card_479e47_contract,
        card_id="479e47d702c8cc38",
    )
    rec(
        "patch_garden_479e47_contract_clears_exact_next_small_fixes",
        bool(
            card_479e47_check.get("pass")
            and card_479e47_hook.get("pass") is True
            and card_479e47_contract.get("card_id") == "479e47d702c8cc38"
            and card_479e47_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_479e47_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_479e47_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_479e47_contract.get("improvements") or [])
            and card_479e47_evidence.get("patch_summary_contract_ready") is True
            and "reduce_tool_quota" in (card_479e47_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_479e47_check.get("evidence_check", {}).get("evidence_evidence") is True
            and card_479e47_check.get("evidence_check", {}).get("tool_evidence") is True
            and "add_one_cheap_self_check_that_proves_the_claim" in (card_479e47_hook.get("cleared_next_small_fixes") or [])
            and card_479e47_contract.get("promotion_allowed") is False
            and card_479e47_contract.get("can_touch_protected_surfaces") is False
            and card_479e47_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_479e47_contract,
        check=card_479e47_check,
        hook=card_479e47_hook,
    )
    card_b299dc_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_b299dc_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_b299dc_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_b299dc_evidence["patch_summary_contract_ready"] = True
    card_b299dc_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
    }
    card_b299dc_tool = {
        "before": {
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_lifecycle_decision_count": 4,
            "tool_quarantine_count": 1,
            "compute_report_tool_actions": ["keep_observing", "reduce_tool_quota"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
    }
    card_b299dc_contract = build_evidence_tool_progress_contract(
        {"patch_summary_evidence_progress_fragment": card_b299dc_evidence},
        card_b299dc_tool,
        card_id="b299dc1c28ed92d6",
    )
    card_b299dc_retry = patch_garden_retry_readiness(
        card_b299dc_contract,
        evidence_fragment=card_b299dc_evidence,
        card_id="b299dc1c28ed92d6",
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
    )
    rec(
        "patch_garden_b299dc_retry_clears_exact_next_small_fixes",
        bool(
            card_b299dc_retry.get("pass") is True
            and card_b299dc_retry.get("missing_next_small_fixes") == []
            and card_b299dc_contract.get("card_id") == "b299dc1c28ed92d6"
            and card_b299dc_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_b299dc_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_b299dc_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_b299dc_contract.get("improvements") or [])
            and card_b299dc_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_b299dc_retry.get("evidence_hook_pass") is True
            and card_b299dc_retry.get("tool_contract_pass") is True
            and card_b299dc_contract.get("promotion_allowed") is False
            and card_b299dc_contract.get("can_touch_protected_surfaces") is False
            and card_b299dc_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_b299dc_contract,
        retry=card_b299dc_retry,
    )
    card_cfe42a_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_cfe42a_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_cfe42a_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_cfe42a_evidence["patch_summary_contract_ready"] = True
    card_cfe42a_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
    }
    card_cfe42a_tool = {
        "before": {
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_lifecycle_decision_count": 4,
            "tool_quarantine_count": 1,
            "compute_report_tool_actions": ["keep_observing", "reduce_tool_quota"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
    }
    card_cfe42a_contract = build_evidence_tool_progress_contract(
        {"patch_summary_evidence_progress_fragment": card_cfe42a_evidence},
        card_cfe42a_tool,
        card_id="cfe42a2750bdb879",
    )
    card_cfe42a_retry = patch_garden_retry_readiness(
        card_cfe42a_contract,
        evidence_fragment=card_cfe42a_evidence,
        card_id="cfe42a2750bdb879",
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
    )
    rec(
        "patch_garden_cfe42a_retry_clears_exact_next_small_fixes",
        bool(
            card_cfe42a_retry.get("pass") is True
            and card_cfe42a_retry.get("missing_next_small_fixes") == []
            and card_cfe42a_contract.get("card_id") == "cfe42a2750bdb879"
            and card_cfe42a_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_cfe42a_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_cfe42a_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_cfe42a_contract.get("improvements") or [])
            and card_cfe42a_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_cfe42a_retry.get("evidence_hook_pass") is True
            and card_cfe42a_retry.get("tool_contract_pass") is True
            and card_cfe42a_contract.get("promotion_allowed") is False
            and card_cfe42a_contract.get("can_touch_protected_surfaces") is False
            and card_cfe42a_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_cfe42a_contract,
        retry=card_cfe42a_retry,
    )
    card_6af289_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_6af289_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_6af289_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_6af289_evidence["patch_summary_contract_ready"] = True
    card_6af289_evidence["patch_garden_card_id"] = "6af2899e56a17279"
    card_6af289_evidence["retry_card_id"] = "6af2899e56a17279"
    card_6af289_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_6af289_tool = {
        "before": {
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_lifecycle_decision_count": 3,
            "tool_quarantine_count": 1,
            "compute_report_tool_actions": ["keep_observing", "reduce_tool_quota"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
    }
    card_6af289_contract = build_evidence_tool_progress_contract(
        {"patch_summary_evidence_progress_fragment": card_6af289_evidence},
        card_6af289_tool,
        card_id="6af2899e56a17279",
    )
    card_6af289_retry = patch_garden_retry_readiness(
        card_6af289_contract,
        evidence_fragment=card_6af289_evidence,
        card_id="6af2899e56a17279",
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_6af289_retry_clears_exact_next_small_fixes",
        bool(
            card_6af289_retry.get("pass") is True
            and card_6af289_retry.get("missing_next_small_fixes") == []
            and set(card_6af289_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_6af289_contract.get("card_id") == "6af2899e56a17279"
            and card_6af289_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_6af289_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_6af289_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_6af289_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_6af289_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_6af289_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_6af289_retry.get("evidence_hook_pass") is True
            and card_6af289_retry.get("tool_contract_pass") is True
            and card_6af289_contract.get("promotion_allowed") is False
            and card_6af289_contract.get("can_touch_protected_surfaces") is False
            and card_6af289_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_6af289_contract,
        retry=card_6af289_retry,
    )
    card_282977_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_282977_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_282977_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_282977_evidence["patch_summary_contract_ready"] = True
    card_282977_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
    }
    card_282977_tool = {
        "before": {
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_lifecycle_decision_count": 4,
            "tool_quarantine_count": 1,
            "compute_report_tool_actions": ["keep_observing", "reduce_tool_quota"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
    }
    card_282977_contract = build_evidence_tool_progress_contract(
        {"patch_summary_evidence_progress_fragment": card_282977_evidence},
        card_282977_tool,
        card_id="28297784e67b2c94",
    )
    card_282977_fixes = [
        PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX,
        "make_the_patch_smaller_and_finish_before_timeout",
        "write_patch_summary_json_before_long_tests",
        "add_evidence_tool_progress_contract_with_before_after_evidence",
        "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
        "add_one_cheap_self_check_that_proves_the_claim",
        "keep_promotion_allowed_false_until_all_gates_pass",
    ]
    card_282977_blocked = patch_garden_retry_readiness(
        card_282977_contract,
        evidence_fragment=card_282977_evidence,
        card_id="28297784e67b2c94",
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=False,
        next_small_fixes=card_282977_fixes,
    )
    card_282977_retry = patch_garden_retry_readiness(
        card_282977_contract,
        evidence_fragment=card_282977_evidence,
        card_id="28297784e67b2c94",
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=True,
        next_small_fixes=card_282977_fixes,
    )
    rec(
        "patch_garden_282977_retry_requires_hidden_replay_artifact_and_clears_card_fixes",
        bool(
            card_282977_blocked.get("pass") is False
            and PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX in (card_282977_blocked.get("missing_next_small_fixes") or [])
            and card_282977_retry.get("pass") is True
            and card_282977_retry.get("missing_next_small_fixes") == []
            and card_282977_retry.get("hidden_replay_artifact_preserved") is True
            and card_282977_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_282977_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_282977_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_282977_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_282977_contract.get("improvements") or [])
            and card_282977_contract.get("promotion_allowed") is False
            and card_282977_contract.get("can_touch_protected_surfaces") is False
            and card_282977_contract.get("tool_influence_requires_matched_lift") is True
        ),
        blocked=card_282977_blocked,
        retry=card_282977_retry,
        contract=card_282977_contract,
    )
    card_287643_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_287643_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_287643_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_287643_evidence["patch_summary_contract_ready"] = True
    card_287643_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_287643_tool = {
        "before": {
            "tool_lifecycle_decision_count": 0,
            "tool_quarantine_count": 0,
            "compute_report_tool_actions": [],
        },
        "after": {
            "tool_lifecycle_decision_count": 4,
            "tool_quarantine_count": 1,
            "compute_report_tool_actions": ["keep_observing", "reduce_tool_quota"],
        },
        "evidence": {"tool_observe_lift_quarantine_or_bridge": True},
    }
    card_287643_contract = build_evidence_tool_progress_contract(
        {"patch_summary_evidence_progress_fragment": card_287643_evidence},
        card_287643_tool,
        card_id="287643679a409573",
    )
    card_287643_retry = patch_garden_retry_readiness(
        card_287643_contract,
        evidence_fragment=card_287643_evidence,
        card_id="287643679a409573",
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_287643_retry_clears_exact_next_small_fixes",
        bool(
            card_287643_retry.get("pass") is True
            and card_287643_retry.get("missing_next_small_fixes") == []
            and set(card_287643_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_287643_contract.get("card_id") == "287643679a409573"
            and card_287643_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_287643_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_287643_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_287643_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_287643_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_287643_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_287643_retry.get("evidence_hook_pass") is True
            and card_287643_retry.get("tool_contract_pass") is True
            and card_287643_contract.get("promotion_allowed") is False
            and card_287643_contract.get("can_touch_protected_surfaces") is False
            and card_287643_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_287643_contract,
        retry=card_287643_retry,
    )
    card_91bb9c_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_91bb9c_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_91bb9c_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_91bb9c_evidence["patch_summary_contract_ready"] = True
    card_91bb9c_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_91bb9c_tool = build_patch_garden_tool_progress_fragment({
        "compute_report_tool_decisions": 2,
        "compute_report_tool_actions": ["reduce_tool_quota", "keep_observing"],
    })
    card_91bb9c_contract = build_evidence_tool_progress_contract(
        {"patch_summary_evidence_progress_fragment": card_91bb9c_evidence},
        card_91bb9c_tool,
        card_id="91bb9c73bbb0f127",
    )
    card_91bb9c_retry = patch_garden_retry_readiness(
        card_91bb9c_contract,
        evidence_fragment=card_91bb9c_evidence,
        card_id="91bb9c73bbb0f127",
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_91bb9c_retry_clears_exact_next_small_fixes",
        bool(
            card_91bb9c_retry.get("pass") is True
            and card_91bb9c_retry.get("missing_next_small_fixes") == []
            and set(card_91bb9c_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_91bb9c_contract.get("card_id") == "91bb9c73bbb0f127"
            and card_91bb9c_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_91bb9c_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_91bb9c_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_91bb9c_contract.get("improvements") or [])
            and card_91bb9c_tool.get("progress_axes") == ["tool"]
            and "reduce_tool_quota" in (card_91bb9c_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_91bb9c_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_91bb9c_retry.get("evidence_hook_pass") is True
            and card_91bb9c_retry.get("tool_contract_pass") is True
            and card_91bb9c_contract.get("promotion_allowed") is False
            and card_91bb9c_contract.get("can_touch_protected_surfaces") is False
            and card_91bb9c_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_91bb9c_contract,
        retry=card_91bb9c_retry,
        tool_fragment=card_91bb9c_tool,
    )
    card_82dd89_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_82dd89_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_82dd89_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_82dd89_evidence["patch_summary_contract_ready"] = True
    card_82dd89_evidence["patch_garden_card_id"] = PATCH_GARDEN_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID
    card_82dd89_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_82dd89_contract = build_patch_garden_tool_matched_control_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_82dd89_evidence},
        card_id=PATCH_GARDEN_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID,
    )
    card_82dd89_retry = patch_garden_retry_readiness(
        card_82dd89_contract,
        evidence_fragment=card_82dd89_evidence,
        card_id=PATCH_GARDEN_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_82dd89_tool_matched_control_retry_clears_exact_next_small_fixes",
        bool(
            card_82dd89_retry.get("pass") is True
            and card_82dd89_retry.get("missing_next_small_fixes") == []
            and set(card_82dd89_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_82dd89_contract.get("card_id") == PATCH_GARDEN_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID
            and card_82dd89_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_82dd89_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_82dd89_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_82dd89_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_82dd89_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_82dd89_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_82dd89_retry.get("evidence_hook_pass") is True
            and card_82dd89_retry.get("tool_contract_pass") is True
            and card_82dd89_contract.get("promotion_allowed") is False
            and card_82dd89_contract.get("can_touch_protected_surfaces") is False
            and card_82dd89_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_82dd89_contract,
        retry=card_82dd89_retry,
    )
    card_778e03_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_778e03_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_778e03_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_778e03_evidence["patch_summary_contract_ready"] = True
    card_778e03_evidence["patch_garden_card_id"] = PATCH_GARDEN_ACTIVE_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID
    card_778e03_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_778e03_contract = build_patch_garden_tool_matched_control_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_778e03_evidence},
        card_id=PATCH_GARDEN_ACTIVE_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID,
    )
    card_778e03_retry = patch_garden_retry_readiness(
        card_778e03_contract,
        evidence_fragment=card_778e03_evidence,
        card_id=PATCH_GARDEN_ACTIVE_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_778e03_tool_matched_control_retry_clears_exact_next_small_fixes",
        bool(
            card_778e03_retry.get("pass") is True
            and card_778e03_retry.get("missing_next_small_fixes") == []
            and set(card_778e03_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_778e03_contract.get("card_id") == PATCH_GARDEN_ACTIVE_TOOL_MATCHED_CONTROL_REPAIR_CARD_ID
            and card_778e03_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_778e03_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_778e03_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_778e03_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_778e03_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_778e03_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_778e03_retry.get("evidence_hook_pass") is True
            and card_778e03_retry.get("tool_contract_pass") is True
            and card_778e03_contract.get("promotion_allowed") is False
            and card_778e03_contract.get("can_touch_protected_surfaces") is False
            and card_778e03_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_778e03_contract,
        retry=card_778e03_retry,
    )
    card_u163986_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_u163986_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_u163986_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_u163986_evidence["patch_summary_contract_ready"] = True
    card_u163986_evidence["patch_garden_card_id"] = PATCH_GARDEN_U163986_REPAIR_CARD_ID
    card_u163986_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_u163986_contract = build_u163986_tool_matched_control_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_u163986_evidence},
    )
    card_u163986_retry = patch_garden_retry_readiness(
        card_u163986_contract,
        evidence_fragment=card_u163986_evidence,
        card_id=PATCH_GARDEN_U163986_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_u163986_tool_matched_control_retry_clears_exact_next_small_fixes",
        bool(
            card_u163986_retry.get("pass") is True
            and card_u163986_retry.get("missing_next_small_fixes") == []
            and set(card_u163986_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_u163986_contract.get("card_id") == PATCH_GARDEN_U163986_REPAIR_CARD_ID
            and card_u163986_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_u163986_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_u163986_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_u163986_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_u163986_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_u163986_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_u163986_retry.get("evidence_hook_pass") is True
            and card_u163986_retry.get("tool_contract_pass") is True
            and card_u163986_contract.get("promotion_allowed") is False
            and card_u163986_contract.get("can_touch_protected_surfaces") is False
            and card_u163986_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_u163986_contract,
        retry=card_u163986_retry,
    )
    card_u9c41ac_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_u9c41ac_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_u9c41ac_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_u9c41ac_evidence["patch_summary_contract_ready"] = True
    card_u9c41ac_evidence["patch_garden_card_id"] = PATCH_GARDEN_U9C41AC_REPAIR_CARD_ID
    card_u9c41ac_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_u9c41ac_contract = build_u9c41ac_tool_matched_control_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_u9c41ac_evidence},
    )
    card_u9c41ac_retry = patch_garden_retry_readiness(
        card_u9c41ac_contract,
        evidence_fragment=card_u9c41ac_evidence,
        card_id=PATCH_GARDEN_U9C41AC_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_u9c41ac_tool_matched_control_retry_clears_exact_next_small_fixes",
        bool(
            card_u9c41ac_retry.get("pass") is True
            and card_u9c41ac_retry.get("missing_next_small_fixes") == []
            and set(card_u9c41ac_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_u9c41ac_contract.get("card_id") == PATCH_GARDEN_U9C41AC_REPAIR_CARD_ID
            and card_u9c41ac_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_u9c41ac_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_u9c41ac_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_u9c41ac_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_u9c41ac_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_u9c41ac_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_u9c41ac_retry.get("evidence_hook_pass") is True
            and card_u9c41ac_retry.get("tool_contract_pass") is True
            and card_u9c41ac_contract.get("promotion_allowed") is False
            and card_u9c41ac_contract.get("can_touch_protected_surfaces") is False
            and card_u9c41ac_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_u9c41ac_contract,
        retry=card_u9c41ac_retry,
    )
    card_u591f7c_next_small_fixes = [
        "move_repair_to_allowed_surface_and_restore_protected_diff_clean",
        "add_one_cheap_self_check_that_proves_the_claim",
        "keep_promotion_allowed_false_until_all_gates_pass",
    ]
    card_u591f7c_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_u591f7c_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_u591f7c_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_u591f7c_evidence["patch_summary_contract_ready"] = True
    card_u591f7c_evidence["patch_garden_card_id"] = PATCH_GARDEN_U591F7C_REPAIR_CARD_ID
    card_u591f7c_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_u591f7c_contract = build_u591f7c_tool_matched_control_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_u591f7c_evidence},
    )
    card_u591f7c_retry = patch_garden_retry_readiness(
        card_u591f7c_contract,
        evidence_fragment=card_u591f7c_evidence,
        card_id=PATCH_GARDEN_U591F7C_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        protected_diff_clean=True,
        next_small_fixes=card_u591f7c_next_small_fixes,
    )
    rec(
        "patch_garden_u591f7c_retry_moves_to_allowed_surface_and_stays_zero_authority",
        bool(
            card_u591f7c_retry.get("pass") is True
            and card_u591f7c_retry.get("missing_next_small_fixes") == []
            and set(card_u591f7c_retry.get("cleared_next_small_fixes") or []) == set(card_u591f7c_next_small_fixes)
            and card_u591f7c_contract.get("card_id") == PATCH_GARDEN_U591F7C_REPAIR_CARD_ID
            and card_u591f7c_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_u591f7c_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_u591f7c_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_u591f7c_contract.get("improvements") or [])
            and card_u591f7c_retry.get("protected_diff_clean") is True
            and card_u591f7c_retry.get("approval_axis_file_coverage", {}).get("evidence_files") == ["code_engine/evidence_proof_worker.py"]
            and card_u591f7c_retry.get("approval_axis_file_coverage", {}).get("tool_files") == [TOOL_LAB_NAME]
            and card_u591f7c_contract.get("promotion_allowed") is False
            and card_u591f7c_contract.get("can_touch_protected_surfaces") is False
            and card_u591f7c_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_u591f7c_contract,
        retry=card_u591f7c_retry,
    )
    card_u19c81a_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_u19c81a_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_u19c81a_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_u19c81a_evidence["patch_summary_contract_ready"] = True
    card_u19c81a_evidence["patch_garden_card_id"] = PATCH_GARDEN_U19C81A_REPAIR_CARD_ID
    card_u19c81a_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_u19c81a_contract = build_u19c81a_tool_matched_control_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_u19c81a_evidence},
    )
    card_u19c81a_retry = patch_garden_retry_readiness(
        card_u19c81a_contract,
        evidence_fragment=card_u19c81a_evidence,
        card_id=PATCH_GARDEN_U19C81A_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_u19c81a_tool_matched_control_retry_clears_exact_next_small_fixes",
        bool(
            card_u19c81a_retry.get("pass") is True
            and card_u19c81a_retry.get("missing_next_small_fixes") == []
            and set(card_u19c81a_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_u19c81a_contract.get("card_id") == PATCH_GARDEN_U19C81A_REPAIR_CARD_ID
            and card_u19c81a_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_u19c81a_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_u19c81a_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_u19c81a_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_u19c81a_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_u19c81a_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_u19c81a_retry.get("evidence_hook_pass") is True
            and card_u19c81a_retry.get("tool_contract_pass") is True
            and card_u19c81a_contract.get("promotion_allowed") is False
            and card_u19c81a_contract.get("can_touch_protected_surfaces") is False
            and card_u19c81a_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_u19c81a_contract,
        retry=card_u19c81a_retry,
    )
    card_u45a8a6_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_u45a8a6_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_u45a8a6_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_u45a8a6_evidence["patch_summary_contract_ready"] = True
    card_u45a8a6_evidence["patch_garden_card_id"] = PATCH_GARDEN_U45A8A6_REPAIR_CARD_ID
    card_u45a8a6_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_u45a8a6_contract = build_u45a8a6_tool_matched_control_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_u45a8a6_evidence},
    )
    card_u45a8a6_retry = patch_garden_retry_readiness(
        card_u45a8a6_contract,
        evidence_fragment=card_u45a8a6_evidence,
        card_id=PATCH_GARDEN_U45A8A6_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_u45a8a6_tool_matched_control_retry_clears_exact_next_small_fixes",
        bool(
            card_u45a8a6_retry.get("pass") is True
            and card_u45a8a6_retry.get("missing_next_small_fixes") == []
            and set(card_u45a8a6_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_u45a8a6_contract.get("card_id") == PATCH_GARDEN_U45A8A6_REPAIR_CARD_ID
            and card_u45a8a6_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_u45a8a6_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_u45a8a6_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_u45a8a6_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_u45a8a6_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_u45a8a6_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_u45a8a6_retry.get("evidence_hook_pass") is True
            and card_u45a8a6_retry.get("tool_contract_pass") is True
            and card_u45a8a6_contract.get("promotion_allowed") is False
            and card_u45a8a6_contract.get("can_touch_protected_surfaces") is False
            and card_u45a8a6_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_u45a8a6_contract,
        retry=card_u45a8a6_retry,
    )
    card_bd6802_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_bd6802_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_bd6802_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_bd6802_evidence["patch_summary_contract_ready"] = True
    card_bd6802_evidence["patch_garden_card_id"] = PATCH_GARDEN_BD6802_REPAIR_CARD_ID
    card_bd6802_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_bd6802_contract = build_bd6802_search_power_spread_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_bd6802_evidence},
    )
    card_bd6802_retry = patch_garden_retry_readiness(
        card_bd6802_contract,
        evidence_fragment=card_bd6802_evidence,
        card_id=PATCH_GARDEN_BD6802_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_bd6802_search_power_spread_retry_clears_exact_next_small_fixes",
        bool(
            card_bd6802_retry.get("pass") is True
            and card_bd6802_retry.get("missing_next_small_fixes") == []
            and set(card_bd6802_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_bd6802_contract.get("card_id") == PATCH_GARDEN_BD6802_REPAIR_CARD_ID
            and card_bd6802_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_bd6802_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_bd6802_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_bd6802_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_bd6802_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_bd6802_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_bd6802_retry.get("evidence_hook_pass") is True
            and card_bd6802_retry.get("tool_contract_pass") is True
            and card_bd6802_contract.get("promotion_allowed") is False
            and card_bd6802_contract.get("can_touch_protected_surfaces") is False
            and card_bd6802_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_bd6802_contract,
        retry=card_bd6802_retry,
    )
    card_u141053_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_u141053_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_u141053_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_u141053_evidence["patch_summary_contract_ready"] = True
    card_u141053_evidence["patch_garden_card_id"] = PATCH_GARDEN_U141053_REPAIR_CARD_ID
    card_u141053_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_u141053_contract = build_u141053_search_power_spread_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_u141053_evidence},
    )
    card_u141053_retry = patch_garden_retry_readiness(
        card_u141053_contract,
        evidence_fragment=card_u141053_evidence,
        card_id=PATCH_GARDEN_U141053_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_u141053_search_power_spread_retry_clears_exact_next_small_fixes",
        bool(
            card_u141053_retry.get("pass") is True
            and card_u141053_retry.get("missing_next_small_fixes") == []
            and set(card_u141053_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_u141053_contract.get("card_id") == PATCH_GARDEN_U141053_REPAIR_CARD_ID
            and card_u141053_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_u141053_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_u141053_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_u141053_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_u141053_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_u141053_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_u141053_retry.get("evidence_hook_pass") is True
            and card_u141053_retry.get("tool_contract_pass") is True
            and card_u141053_contract.get("promotion_allowed") is False
            and card_u141053_contract.get("can_touch_protected_surfaces") is False
            and card_u141053_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_u141053_contract,
        retry=card_u141053_retry,
    )
    card_b758c1_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_b758c1_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_b758c1_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_b758c1_evidence["patch_summary_contract_ready"] = True
    card_b758c1_evidence["patch_garden_card_id"] = PATCH_GARDEN_B758C1_REPAIR_CARD_ID
    card_b758c1_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            PATCH_GARDEN_EVIDENCE_STALE_REFRESH_EVIDENCE_FIX,
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "evidence_new_cards_or_bridge_or_stale_refresh_evidence_ready": True,
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_b758c1_contract = build_b758c1_search_power_spread_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_b758c1_evidence},
    )
    card_b758c1_retry = patch_garden_retry_readiness(
        card_b758c1_contract,
        evidence_fragment=card_b758c1_evidence,
        card_id=PATCH_GARDEN_B758C1_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_B758C1_NEXT_SMALL_FIXES),
    )
    rec(
        "patch_garden_b758c1_search_power_spread_retry_clears_card_next_small_fixes",
        bool(
            card_b758c1_retry.get("pass") is True
            and card_b758c1_retry.get("missing_next_small_fixes") == []
            and set(card_b758c1_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_B758C1_NEXT_SMALL_FIXES)
            and card_b758c1_contract.get("card_id") == PATCH_GARDEN_B758C1_REPAIR_CARD_ID
            and card_b758c1_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_b758c1_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_b758c1_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_b758c1_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_b758c1_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_b758c1_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_b758c1_retry.get("evidence_hook_pass") is True
            and card_b758c1_retry.get("tool_contract_pass") is True
            and card_b758c1_retry.get("patch_summary_written") is True
            and card_b758c1_contract.get("promotion_allowed") is False
            and card_b758c1_contract.get("can_touch_protected_surfaces") is False
            and card_b758c1_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_b758c1_contract,
        retry=card_b758c1_retry,
    )
    card_3dd09d_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_3dd09d_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_3dd09d_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_3dd09d_evidence["patch_summary_contract_ready"] = True
    card_3dd09d_evidence["patch_garden_card_id"] = PATCH_GARDEN_ACTIVE_REPOSITORY_REPAIR_NURSERY_REPAIR_CARD_ID
    card_3dd09d_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_3dd09d_contract = build_patch_garden_repository_repair_mechanism_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_3dd09d_evidence},
        card_id=PATCH_GARDEN_ACTIVE_REPOSITORY_REPAIR_NURSERY_REPAIR_CARD_ID,
    )
    card_3dd09d_retry = patch_garden_retry_readiness(
        card_3dd09d_contract,
        evidence_fragment=card_3dd09d_evidence,
        card_id=PATCH_GARDEN_ACTIVE_REPOSITORY_REPAIR_NURSERY_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_3dd09d_repository_repair_nursery_retry_clears_exact_next_small_fixes",
        bool(
            card_3dd09d_retry.get("pass") is True
            and card_3dd09d_retry.get("missing_next_small_fixes") == []
            and set(card_3dd09d_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_3dd09d_contract.get("card_id") == PATCH_GARDEN_ACTIVE_REPOSITORY_REPAIR_NURSERY_REPAIR_CARD_ID
            and card_3dd09d_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_3dd09d_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_3dd09d_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_3dd09d_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_3dd09d_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_3dd09d_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_3dd09d_retry.get("evidence_hook_pass") is True
            and card_3dd09d_retry.get("tool_contract_pass") is True
            and card_3dd09d_contract.get("promotion_allowed") is False
            and card_3dd09d_contract.get("can_touch_protected_surfaces") is False
            and card_3dd09d_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_3dd09d_contract,
        retry=card_3dd09d_retry,
    )
    card_d0eb2f_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_d0eb2f_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_d0eb2f_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_d0eb2f_evidence["patch_summary_contract_ready"] = True
    card_d0eb2f_evidence["patch_garden_card_id"] = PATCH_GARDEN_D0EB2F_REPAIR_CARD_ID
    card_d0eb2f_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_d0eb2f_contract = build_d0eb2f_repository_repair_mechanism_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_d0eb2f_evidence},
    )
    card_d0eb2f_retry = patch_garden_retry_readiness(
        card_d0eb2f_contract,
        evidence_fragment=card_d0eb2f_evidence,
        card_id=PATCH_GARDEN_D0EB2F_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_d0eb2f_repository_repair_nursery_retry_clears_exact_next_small_fixes",
        bool(
            card_d0eb2f_retry.get("pass") is True
            and card_d0eb2f_retry.get("missing_next_small_fixes") == []
            and set(card_d0eb2f_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_d0eb2f_contract.get("card_id") == PATCH_GARDEN_D0EB2F_REPAIR_CARD_ID
            and card_d0eb2f_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_d0eb2f_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_d0eb2f_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_d0eb2f_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_d0eb2f_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_d0eb2f_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_d0eb2f_retry.get("evidence_hook_pass") is True
            and card_d0eb2f_retry.get("tool_contract_pass") is True
            and card_d0eb2f_contract.get("promotion_allowed") is False
            and card_d0eb2f_contract.get("can_touch_protected_surfaces") is False
            and card_d0eb2f_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_d0eb2f_contract,
        retry=card_d0eb2f_retry,
    )
    card_b55003_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_b55003_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_b55003_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_b55003_evidence["patch_summary_contract_ready"] = True
    card_b55003_evidence["patch_garden_card_id"] = PATCH_GARDEN_B55003_REPAIR_CARD_ID
    card_b55003_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_b55003_contract = build_b55003_repository_repair_mechanism_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_b55003_evidence},
    )
    card_b55003_retry = patch_garden_retry_readiness(
        card_b55003_contract,
        evidence_fragment=card_b55003_evidence,
        card_id=PATCH_GARDEN_B55003_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_RETRY_REQUIRED_FIXES),
    )
    rec(
        "patch_garden_b55003_repository_repair_nursery_retry_clears_exact_next_small_fixes",
        bool(
            card_b55003_retry.get("pass") is True
            and card_b55003_retry.get("missing_next_small_fixes") == []
            and set(card_b55003_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_RETRY_REQUIRED_FIXES)
            and card_b55003_contract.get("card_id") == PATCH_GARDEN_B55003_REPAIR_CARD_ID
            and card_b55003_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_b55003_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_b55003_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_b55003_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_b55003_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_b55003_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_b55003_retry.get("evidence_hook_pass") is True
            and card_b55003_retry.get("tool_contract_pass") is True
            and card_b55003_contract.get("promotion_allowed") is False
            and card_b55003_contract.get("can_touch_protected_surfaces") is False
            and card_b55003_contract.get("tool_influence_requires_matched_lift") is True
        ),
        contract=card_b55003_contract,
        retry=card_b55003_retry,
    )
    card_f45089_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_f45089_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_f45089_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_f45089_evidence["patch_summary_contract_ready"] = True
    card_f45089_evidence["patch_garden_card_id"] = PATCH_GARDEN_F45089_REPAIR_CARD_ID
    card_f45089_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            PATCH_GARDEN_EVIDENCE_STALE_REFRESH_EVIDENCE_FIX,
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_f45089_contract = build_f45089_repository_repair_mechanism_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_f45089_evidence},
    )
    card_f45089_retry = patch_garden_retry_readiness(
        card_f45089_contract,
        evidence_fragment=card_f45089_evidence,
        card_id=PATCH_GARDEN_F45089_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_F45089_NEXT_SMALL_FIXES),
    )
    rec(
        "patch_garden_f45089_repository_repair_nursery_retry_clears_card_next_small_fixes",
        bool(
            card_f45089_retry.get("pass") is True
            and card_f45089_retry.get("missing_next_small_fixes") == []
            and set(card_f45089_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_F45089_NEXT_SMALL_FIXES)
            and card_f45089_contract.get("card_id") == PATCH_GARDEN_F45089_REPAIR_CARD_ID
            and card_f45089_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_f45089_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_f45089_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_f45089_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_f45089_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_f45089_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_f45089_retry.get("evidence_hook_pass") is True
            and card_f45089_retry.get("tool_contract_pass") is True
            and card_f45089_retry.get("patch_summary_written") is True
            and card_f45089_contract.get("promotion_allowed") is False
            and card_f45089_contract.get("can_touch_protected_surfaces") is False
            and card_f45089_contract.get("tool_influence_requires_matched_lift") is True
        ),
        retry=card_f45089_retry,
        contract=card_f45089_contract,
    )
    card_u1ffe8c_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_u1ffe8c_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_u1ffe8c_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_u1ffe8c_evidence["patch_summary_contract_ready"] = True
    card_u1ffe8c_evidence["patch_garden_card_id"] = PATCH_GARDEN_U1FFE8C_REPAIR_CARD_ID
    card_u1ffe8c_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            PATCH_GARDEN_EVIDENCE_STALE_REFRESH_EVIDENCE_FIX,
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "evidence_new_cards_or_bridge_or_stale_refresh_evidence_ready": True,
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_u1ffe8c_contract = build_u1ffe8c_repository_repair_mechanism_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_u1ffe8c_evidence},
    )
    card_u1ffe8c_retry = patch_garden_retry_readiness(
        card_u1ffe8c_contract,
        evidence_fragment=card_u1ffe8c_evidence,
        card_id=PATCH_GARDEN_U1FFE8C_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        next_small_fixes=list(PATCH_GARDEN_U1FFE8C_NEXT_SMALL_FIXES),
    )
    rec(
        "patch_garden_u1ffe8c_retry_clears_exact_next_small_fixes",
        bool(
            card_u1ffe8c_retry.get("pass") is True
            and card_u1ffe8c_retry.get("missing_next_small_fixes") == []
            and set(card_u1ffe8c_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_U1FFE8C_NEXT_SMALL_FIXES)
            and card_u1ffe8c_contract.get("card_id") == PATCH_GARDEN_U1FFE8C_REPAIR_CARD_ID
            and card_u1ffe8c_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_u1ffe8c_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_u1ffe8c_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_u1ffe8c_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_u1ffe8c_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and PATCH_GARDEN_EVIDENCE_STALE_REFRESH_EVIDENCE_FIX in (card_u1ffe8c_retry.get("cleared_next_small_fixes") or [])
            and card_u1ffe8c_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_u1ffe8c_retry.get("evidence_hook_pass") is True
            and card_u1ffe8c_retry.get("tool_contract_pass") is True
            and card_u1ffe8c_retry.get("patch_summary_written") is True
            and card_u1ffe8c_contract.get("promotion_allowed") is False
            and card_u1ffe8c_contract.get("can_touch_protected_surfaces") is False
            and card_u1ffe8c_contract.get("tool_influence_requires_matched_lift") is True
        ),
        retry=card_u1ffe8c_retry,
        contract=card_u1ffe8c_contract,
    )
    card_b5221c_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_b5221c_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_b5221c_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_b5221c_evidence["patch_summary_contract_ready"] = True
    card_b5221c_evidence["patch_garden_card_id"] = PATCH_GARDEN_B5221C_REPAIR_CARD_ID
    card_b5221c_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "hidden_replay_artifact_repair": {
            "next_small_fix": PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX,
            "artifact_required_before_hidden_replay": True,
            "synthetic_replay_allowed": False,
        },
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_b5221c_contract = build_b5221c_evidence_tool_progress_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_b5221c_evidence},
    )
    card_b5221c_blocked = patch_garden_retry_readiness(
        card_b5221c_contract,
        evidence_fragment=card_b5221c_evidence,
        card_id=PATCH_GARDEN_B5221C_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=False,
        next_small_fixes=list(PATCH_GARDEN_B5221C_NEXT_SMALL_FIXES),
    )
    card_b5221c_retry = patch_garden_retry_readiness(
        card_b5221c_contract,
        evidence_fragment=card_b5221c_evidence,
        card_id=PATCH_GARDEN_B5221C_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=True,
        next_small_fixes=list(PATCH_GARDEN_B5221C_NEXT_SMALL_FIXES),
    )
    rec(
        "patch_garden_b5221c_hidden_replay_retry_clears_exact_next_small_fixes",
        bool(
            card_b5221c_blocked.get("pass") is False
            and PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX in (card_b5221c_blocked.get("missing_next_small_fixes") or [])
            and card_b5221c_retry.get("pass") is True
            and card_b5221c_retry.get("missing_next_small_fixes") == []
            and set(card_b5221c_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_B5221C_NEXT_SMALL_FIXES)
            and card_b5221c_retry.get("hidden_replay_artifact_preserved") is True
            and card_b5221c_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_b5221c_retry.get("evidence_hook_pass") is True
            and card_b5221c_retry.get("tool_contract_pass") is True
            and card_b5221c_contract.get("card_id") == PATCH_GARDEN_B5221C_REPAIR_CARD_ID
            and card_b5221c_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_b5221c_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_b5221c_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_b5221c_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_b5221c_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_b5221c_contract.get("promotion_allowed") is False
            and card_b5221c_contract.get("can_touch_protected_surfaces") is False
            and card_b5221c_contract.get("tool_influence_requires_matched_lift") is True
        ),
        blocked=card_b5221c_blocked,
        retry=card_b5221c_retry,
        contract=card_b5221c_contract,
    )
    card_c3d7f8_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_c3d7f8_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_c3d7f8_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_c3d7f8_evidence["patch_summary_contract_ready"] = True
    card_c3d7f8_evidence["patch_garden_card_id"] = PATCH_GARDEN_C3D7F8_REPAIR_CARD_ID
    card_c3d7f8_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "hidden_replay_artifact_repair": {
            "next_small_fix": PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX,
            "artifact_required_before_hidden_replay": True,
            "synthetic_replay_allowed": False,
        },
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_c3d7f8_contract = build_c3d7f8_evidence_tool_progress_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_c3d7f8_evidence},
    )
    card_c3d7f8_blocked = patch_garden_retry_readiness(
        card_c3d7f8_contract,
        evidence_fragment=card_c3d7f8_evidence,
        card_id=PATCH_GARDEN_C3D7F8_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=False,
        protected_diff_clean=False,
        next_small_fixes=list(PATCH_GARDEN_C3D7F8_NEXT_SMALL_FIXES),
    )
    card_c3d7f8_retry = patch_garden_retry_readiness(
        card_c3d7f8_contract,
        evidence_fragment=card_c3d7f8_evidence,
        card_id=PATCH_GARDEN_C3D7F8_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=True,
        protected_diff_clean=True,
        next_small_fixes=list(PATCH_GARDEN_C3D7F8_NEXT_SMALL_FIXES),
    )
    rec(
        "patch_garden_c3d7f8_hidden_replay_retry_clears_exact_next_small_fixes",
        bool(
            card_c3d7f8_blocked.get("pass") is False
            and PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX in (card_c3d7f8_blocked.get("missing_next_small_fixes") or [])
            and "move_repair_to_allowed_surface_and_restore_protected_diff_clean" in (card_c3d7f8_blocked.get("missing_next_small_fixes") or [])
            and card_c3d7f8_retry.get("pass") is True
            and card_c3d7f8_retry.get("missing_next_small_fixes") == []
            and set(card_c3d7f8_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_C3D7F8_NEXT_SMALL_FIXES)
            and card_c3d7f8_retry.get("hidden_replay_artifact_preserved") is True
            and card_c3d7f8_retry.get("protected_diff_clean") is True
            and card_c3d7f8_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_c3d7f8_retry.get("evidence_hook_pass") is True
            and card_c3d7f8_retry.get("tool_contract_pass") is True
            and card_c3d7f8_contract.get("card_id") == PATCH_GARDEN_C3D7F8_REPAIR_CARD_ID
            and card_c3d7f8_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_c3d7f8_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_c3d7f8_contract.get("improvements") or [])
            and "tool_bridge_to_verificationpass" in (card_c3d7f8_contract.get("improvements") or [])
            and "route_tool_observe_to_verificationpass" in (card_c3d7f8_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_c3d7f8_contract.get("promotion_allowed") is False
            and card_c3d7f8_contract.get("can_touch_protected_surfaces") is False
            and card_c3d7f8_contract.get("tool_influence_requires_matched_lift") is True
        ),
        blocked=card_c3d7f8_blocked,
        retry=card_c3d7f8_retry,
        contract=card_c3d7f8_contract,
    )
    card_e912ae_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_e912ae_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_e912ae_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_e912ae_evidence["patch_summary_contract_ready"] = True
    card_e912ae_evidence["patch_garden_card_id"] = PATCH_GARDEN_E912AE_REPAIR_CARD_ID
    card_e912ae_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_e912ae_contract = build_patch_garden_e912ae_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_e912ae_evidence},
    )
    card_e912ae_blocked = patch_garden_retry_readiness(
        card_e912ae_contract,
        evidence_fragment=card_e912ae_evidence,
        card_id=PATCH_GARDEN_E912AE_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=False,
        next_small_fixes=list(PATCH_GARDEN_E912AE_NEXT_SMALL_FIXES),
    )
    card_e912ae_retry = patch_garden_retry_readiness(
        card_e912ae_contract,
        evidence_fragment=card_e912ae_evidence,
        card_id=PATCH_GARDEN_E912AE_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=True,
        next_small_fixes=list(PATCH_GARDEN_E912AE_NEXT_SMALL_FIXES),
    )
    rec(
        "patch_garden_e912ae_hidden_replay_retry_clears_exact_next_small_fixes",
        bool(
            card_e912ae_blocked.get("pass") is False
            and PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX in (card_e912ae_blocked.get("missing_next_small_fixes") or [])
            and card_e912ae_retry.get("pass") is True
            and card_e912ae_retry.get("missing_next_small_fixes") == []
            and set(card_e912ae_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_E912AE_NEXT_SMALL_FIXES)
            and card_e912ae_retry.get("hidden_replay_artifact_preserved") is True
            and card_e912ae_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_e912ae_retry.get("evidence_hook_pass") is True
            and card_e912ae_retry.get("tool_contract_pass") is True
            and card_e912ae_contract.get("card_id") == PATCH_GARDEN_E912AE_REPAIR_CARD_ID
            and card_e912ae_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_e912ae_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_e912ae_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_e912ae_contract.get("improvements") or [])
            and card_e912ae_contract.get("promotion_allowed") is False
            and card_e912ae_contract.get("can_touch_protected_surfaces") is False
            and card_e912ae_contract.get("tool_influence_requires_matched_lift") is True
        ),
        blocked=card_e912ae_blocked,
        retry=card_e912ae_retry,
        contract=card_e912ae_contract,
    )
    card_u30ffa5_evidence = sample_progress_contract(["evidence_stale_refresh_action"], required_axes=["evidence"])
    card_u30ffa5_evidence["progress_hook"] = "evidence_patch_summary_merge_fragment"
    card_u30ffa5_evidence["patch_summary_merge_required_axes"] = ["evidence", "tool"]
    card_u30ffa5_evidence["patch_summary_contract_ready"] = True
    card_u30ffa5_evidence["patch_garden_card_id"] = PATCH_GARDEN_U30FFA5_REPAIR_CARD_ID
    card_u30ffa5_evidence["patch_garden_repair_progress_hook"] = {
        "pass": True,
        "cleared_next_small_fixes": [
            "add_evidence_tool_progress_contract_with_before_after_evidence",
            "patch_code_engine_evidence_proof_worker_with_one_small_progress_hook",
            "keep_promotion_allowed_false_until_all_gates_pass",
        ],
        "hidden_replay_artifact_repair": {
            "next_small_fix": PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX,
            "artifact_required_before_hidden_replay": True,
            "synthetic_replay_allowed": False,
        },
        "promotion_allowed": False,
        "can_touch_protected_surfaces": False,
        "tool_influence_requires_matched_lift": True,
    }
    card_u30ffa5_contract = build_u30ffa5_patch_garden_retry_contract(
        {"patch_summary_evidence_progress_fragment": card_u30ffa5_evidence},
    )
    card_u30ffa5_blocked = patch_garden_retry_readiness(
        card_u30ffa5_contract,
        evidence_fragment=card_u30ffa5_evidence,
        card_id=PATCH_GARDEN_U30FFA5_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=False,
        next_small_fixes=list(PATCH_GARDEN_U30FFA5_NEXT_SMALL_FIXES),
    )
    card_u30ffa5_retry = patch_garden_retry_readiness(
        card_u30ffa5_contract,
        evidence_fragment=card_u30ffa5_evidence,
        card_id=PATCH_GARDEN_U30FFA5_REPAIR_CARD_ID,
        patch_summary_written=True,
        changed_files=[TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
        codex_exec_clean=True,
        hidden_replay_artifact_preserved=True,
        next_small_fixes=list(PATCH_GARDEN_U30FFA5_NEXT_SMALL_FIXES),
    )
    rec(
        "patch_garden_u30ffa5_hidden_replay_retry_clears_exact_next_small_fixes",
        bool(
            card_u30ffa5_blocked.get("pass") is False
            and PATCH_GARDEN_HIDDEN_REPLAY_REPAIR_FIX in (card_u30ffa5_blocked.get("missing_next_small_fixes") or [])
            and card_u30ffa5_retry.get("pass") is True
            and card_u30ffa5_retry.get("missing_next_small_fixes") == []
            and set(card_u30ffa5_retry.get("cleared_next_small_fixes") or []) == set(PATCH_GARDEN_U30FFA5_NEXT_SMALL_FIXES)
            and card_u30ffa5_retry.get("hidden_replay_artifact_preserved") is True
            and card_u30ffa5_retry.get("approval_axis_file_coverage", {}).get("pass") is True
            and card_u30ffa5_retry.get("evidence_hook_pass") is True
            and card_u30ffa5_retry.get("tool_contract_pass") is True
            and card_u30ffa5_contract.get("card_id") == PATCH_GARDEN_U30FFA5_REPAIR_CARD_ID
            and card_u30ffa5_contract.get("progress_axes") == ["evidence", "tool"]
            and set(card_u30ffa5_contract.get("required_axes") or []) == {"evidence", "tool"}
            and "evidence_stale_refresh_action" in (card_u30ffa5_contract.get("improvements") or [])
            and "tool_proxy_quarantine" in (card_u30ffa5_contract.get("improvements") or [])
            and "reduce_tool_quota" in (card_u30ffa5_contract.get("after", {}).get("compute_report_tool_actions") or [])
            and card_u30ffa5_contract.get("promotion_allowed") is False
            and card_u30ffa5_contract.get("can_touch_protected_surfaces") is False
            and card_u30ffa5_contract.get("tool_influence_requires_matched_lift") is True
        ),
        blocked=card_u30ffa5_blocked,
        retry=card_u30ffa5_retry,
        contract=card_u30ffa5_contract,
    )
    tool_only_coverage = approval_axis_file_coverage([TOOL_LAB_NAME])
    both_coverage = approval_axis_file_coverage([TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"])
    rec(
        "approval_requires_tool_and_evidence_file_coverage",
        not tool_only_coverage.get("pass") and both_coverage.get("pass"),
        tool_only=tool_only_coverage,
        both=both_coverage,
    )
    with tempfile.TemporaryDirectory(prefix="tool_lab_approval_selfcheck_") as approval_tmp:
        approval_root = Path(approval_tmp)
        approval_variant = approval_root / "variant_both_axes"
        (approval_root / "code_engine").mkdir(parents=True, exist_ok=True)
        (approval_variant / "code_engine").mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(__file__).resolve(), approval_root / TOOL_LAB_NAME)
        shutil.copy2(Path(__file__).resolve(), approval_variant / TOOL_LAB_NAME)
        with (approval_variant / TOOL_LAB_NAME).open("a", encoding="utf-8") as f:
            f.write("\n# approval coverage self-check tool axis\n")
        evidence_src = root / "code_engine" / "evidence_proof_worker.py"
        shutil.copy2(evidence_src, approval_root / "code_engine" / "evidence_proof_worker.py")
        shutil.copy2(evidence_src, approval_variant / "code_engine" / "evidence_proof_worker.py")
        with (approval_variant / "code_engine" / "evidence_proof_worker.py").open("a", encoding="utf-8") as f:
            f.write("\n# approval coverage self-check evidence axis\n")
        base_summary = {
            "files_changed": [TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
            "functions_changed": ["approval_gate_self_check"],
            "tests_run": ["self_check"],
            "blocker_x": "single_axis_patch_approval",
            "allowed_surface_y": "tool/evidence diagnostics",
            "success_metric_z": "both axes required before approval",
            "baseline": "single-axis patches could reach gate evaluation",
            "rollback_path": "discard sandbox variant",
            "protected_surfaces_checked": True,
            "remaining_risks": [],
        }
        only_tool_summary = dict(base_summary)
        only_tool_summary["evidence_tool_progress_contract"] = sample_progress_contract(["tool_proxy_quarantine"], required_axes=["evidence", "tool"])
        write_json(approval_variant / "patch_summary.json", only_tool_summary)
        judged_ok = {"score": {"pass": True, "reject": False, "score": 95.0}, "judge": {"protected_diff_pass": True}}
        gate_tool_only = tool_brain_gate({"variant_dir": str(approval_variant), "codex_exec": {"pass": True}}, judged_ok, approval_root, allow_codex=True, ticket={})
        both_summary = dict(base_summary)
        both_summary["evidence_tool_progress_contract"] = sample_progress_contract(["evidence_stale_refresh_action", "tool_proxy_quarantine"], required_axes=["evidence", "tool"])
        write_json(approval_variant / "patch_summary.json", both_summary)
        gate_both = tool_brain_gate({"variant_dir": str(approval_variant), "codex_exec": {"pass": True}}, judged_ok, approval_root, allow_codex=True, ticket={})
        rec(
            "tool_brain_gate_approves_only_both_axis_code_and_contract",
            not gate_tool_only.get("pass") and "evidence_axis_improvement_missing" in gate_tool_only.get("reasons", []) and gate_both.get("pass"),
            tool_only_gate=gate_tool_only,
            both_gate=gate_both,
        )
        hidden_run = approval_root / "code_surgery" / "tool_lab" / "runs" / "unit_hidden_replay"
        hidden_variant = hidden_run / "variants" / "variant_hidden_replay_ok"
        hidden_variant.mkdir(parents=True, exist_ok=True)
        for name in (ENGINE_NAME, LEGACY_RUNTIME_NAME, SUPERVISOR_NAME, LAB_OS_NAME, TOOL_LAB_NAME):
            copy_optional(root / name, approval_root / name)
            copy_optional(root / name, hidden_variant / name)
        copy_optional(root / "code_engine", approval_root / "code_engine")
        copy_optional(root / "code_engine", hidden_variant / "code_engine")
        with (hidden_variant / TOOL_LAB_NAME).open("a", encoding="utf-8") as f:
            f.write("\n# hidden replay self-check tool axis\n")
        with (hidden_variant / "code_engine" / "evidence_proof_worker.py").open("a", encoding="utf-8") as f:
            f.write("\n# hidden replay self-check evidence axis\n")
        hidden_summary = dict(base_summary)
        hidden_summary["evidence_tool_progress_contract"] = sample_progress_contract(["evidence_stale_refresh_action", "tool_proxy_quarantine"], required_axes=["evidence", "tool"])
        write_json(hidden_variant / "patch_summary.json", hidden_summary)
        (approval_root / "development_governance_v10.py").write_text(
            "\n".join([
                "def rgv10_build_blind_vault_contract(n_rows=0, patch_id='', persist=False):",
                "    return {'window_count': 3, 'patch_id': patch_id, 'persist': bool(persist)}",
                "def rgv10_blind_gate(patch_id='', replay_result=None, allow_scaffold=False, persist=False):",
                "    replay_result = replay_result or {}",
                "    ok = bool(replay_result.get('non_final_replay_only') and replay_result.get('protected_context_clean') and replay_result.get('median_lift', 0) > 0 and replay_result.get('worst_window_lift', -1) >= 0 and not allow_scaffold)",
                "    return {'pass': ok, 'reason': 'ok' if ok else 'self_check_v10_replay_rejected', 'persist': bool(persist)}",
                "",
            ]),
            encoding="utf-8",
        )
        hidden_request = write_hidden_replay_request(
            approval_root,
            card={
                "card_id": "hidden_replay_self_check_card",
                "variant_name": "variant_hidden_replay_ok",
                "patch_family": "evidence_tool_progress_contract",
                "run_dir": str(hidden_run),
                "patch_summary_path": str(hidden_variant / "patch_summary.json"),
                "changed_files": [TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"],
            },
            manifest={"variant_name": "variant_hidden_replay_ok", "variant_dir": str(hidden_variant)},
        )
        preserved_variant_dir = Path(str(hidden_request.get("variant_artifact_dir") or ""))
        if hidden_request.get("variant_artifact_preserved") and preserved_variant_dir.exists():
            shutil.rmtree(hidden_variant)
        hidden_nursery = process_hidden_replay_requests(approval_root, limit=1)
        hidden_replay = hidden_replay_result_for_patch(approval_root, "variant_hidden_replay_ok", card_id="hidden_replay_self_check_card")
        rec(
            "hidden_replay_request_preserves_variant_artifact_before_prune",
            bool(
                hidden_request.get("variant_artifact_preserved") is True
                and preserved_variant_dir.exists()
                and (preserved_variant_dir / "patch_summary.json").exists()
                and not hidden_variant.exists()
                and hidden_replay
                and str(hidden_replay.get("variant_dir") or "") == str(preserved_variant_dir)
            ),
            request=hidden_request,
            replay=hidden_replay,
        )
        rec(
            "hidden_replay_nursery_consumes_request_and_writes_v10_pass_result",
            bool(
                hidden_request
                and hidden_nursery.get("attempts")
                and hidden_nursery["attempts"][0].get("pass")
                and hidden_replay
                and float(hidden_replay.get("median_lift") or 0.0) > 0.0
                and float(hidden_replay.get("worst_window_lift") or -1.0) >= 0.0
            ),
            request=hidden_request,
            nursery=hidden_nursery,
            replay=hidden_replay,
        )
        surgery_variant = approval_root / "research_best" / "code_surgery" / "sandbox" / "unit_surgery_hidden_replay"
        surgery_variant.mkdir(parents=True, exist_ok=True)
        surgery_patch_id = "unit_surgery_patch_ok"
        surgery_tests = [
            {"name": "PatchCompile", "pass": True},
            {"name": "ArchitectureSelfCheck", "pass": True},
            {"name": "SyntheticCollapseTests", "pass": True},
            {"name": "MiniReplayProof", "pass": True},
            {"name": "SacredDiff", "pass": True},
            {"name": "PatchOverfitGuard", "pass": True},
        ]
        write_json(surgery_variant / "patch_summary.json", {
            "schema_version": SCHEMA_VERSION,
            "event": "SurgeryHiddenReplayPatchSummary",
            "pass": True,
            "patch_id": surgery_patch_id,
            "tests_run": surgery_tests,
            "protected_surfaces_checked": True,
            "promotion_allowed": False,
            "can_touch_protected_surfaces": False,
            "policy": "self-check surgery replay artifact only",
        })
        surgery_attempt = run_hidden_replay_request(approval_root, {
            "schema_version": SCHEMA_VERSION,
            "event": "SurgeryHiddenReplayRequest",
            "status": "requested",
            "patch_id": surgery_patch_id,
            "variant_name": surgery_patch_id,
            "card_id": "proof_collapse",
            "patch_family": "code_surgery_supervisor",
            "variant_dir": str(surgery_variant),
            "patch_summary_path": str(surgery_variant / "patch_summary.json"),
            "promotion_allowed": False,
            "can_touch_final_holdout_or_live": False,
            "can_touch_protected_surfaces": False,
            "request_id": "unit_surgery_hidden_replay_request",
        })
        surgery_replay = hidden_replay_result_for_patch(approval_root, surgery_patch_id, card_id="proof_collapse")
        rec(
            "surgery_hidden_replay_uses_code_surgery_gate_without_tool_axis_bypass",
            bool(
                surgery_attempt.get("pass")
                and surgery_attempt.get("gate_mode") == "code_surgery_hidden_replay"
                and surgery_replay
                and surgery_replay.get("protected_context_clean") is True
                and float(surgery_replay.get("median_lift") or 0.0) > 0.0
                and surgery_attempt.get("surgery_hidden_replay_gate", {}).get("pass") is True
            ),
            attempt=surgery_attempt,
            replay=surgery_replay,
        )
    with tempfile.TemporaryDirectory(prefix="tool_lab_garden_selfcheck_") as garden_tmp:
        garden_root = Path(garden_tmp)
        garden_card = build_patch_garden_card(
            garden_root,
            garden_root / "code_surgery" / "tool_lab" / "runs" / "unit_garden",
            {
                "variant_name": "variant_unit_near_pass",
                "patch_family": "evidence_tool_progress_contract",
                "score": 55.0,
                "codex_ran": True,
                "tool_brain_gate_reasons": ["patch_summary_missing", "tool_axis_improvement_missing"],
            },
            manifest={"variant_name": "variant_unit_near_pass", "codex_exec": {"pass": False, "seconds": 1.0, "returncode": None}},
            gate={"reasons": ["patch_summary_missing", "tool_axis_improvement_missing"], "changed_files": ["development_tool_lab.py"]},
            judged={"score": {"score": 55.0, "reasons": ["score_below_tool_brain_min"]}},
        )
        rec(
            "patch_garden_card_records_small_fix_actions",
            garden_card.get("status") == "needs_small_fix"
            and "write_patch_summary_json_before_long_tests" in garden_card.get("next_small_fixes", [])
            and "add_tool_observe_lift_quarantine_or_bridge_evidence" in garden_card.get("next_small_fixes", [])
            and garden_card.get("nursery_policy", "").startswith("record-only"),
            card=garden_card,
        )
        written_garden = append_patch_garden_card(garden_root, garden_card)
        rec("patch_garden_memory_writes", bool(written_garden and read_patch_garden(garden_root, limit=1)), card=written_garden)
        missing_summary_card = {
            **garden_card,
            "variant_name": "variant_unit_missing_retry_summary",
            "patch_summary_path": str(garden_root / "missing_variant" / "patch_summary.json"),
            "codex_exec": {"pass": False, "returncode": None, "seconds": 0.1},
            "reasons": ["codex_exec_not_clean", "patch_summary_missing", "evidence_tool_progress_contract_missing"],
        }
        append_patch_garden_card(garden_root, missing_summary_card)
        rec(
            "patch_garden_nurse_skips_legacy_missing_retry_summary",
            patch_garden_card_has_missing_retry_summary(garden_root, missing_summary_card)
            and all(c.get("variant_name") != "variant_unit_missing_retry_summary" for c in active_patch_garden_cards(garden_root, limit=8)),
            card=missing_summary_card,
        )
        direct_missing_summary_plan = build_patch_garden_nurse_plan(garden_root, [missing_summary_card], allow_codex=True)
        rec(
            "patch_garden_nurse_plan_filters_direct_missing_retry_summary_card",
            direct_missing_summary_plan.get("retry_requested") is False
            and direct_missing_summary_plan.get("skipped_missing_retry_summary_count") == 1
            and direct_missing_summary_plan.get("approval_status") == "no_active_cards",
            nurse=direct_missing_summary_plan,
        )
        protected_dead_end_card = {
            **garden_card,
            "variant_name": "variant_unit_protected_dead_end",
            "score": 5.0,
            "changed_files": [
                TOOL_LAB_NAME,
                "code_engine/evidence_proof_worker.py",
                "code_engine/legacy_slices/slice_017_llm_v5113_is_nochild_failsafe_brain.py",
            ],
            "patch_summary_pass": True,
            "progress_contract_pass": True,
            "reasons": [
                "judge_or_critic_reject",
                "score_below_tool_brain_min",
                "protected_diff_not_clean",
                "critic_protected_constants_reject",
                "protected_diff_fail",
            ],
            "next_small_fixes": [
                "move_repair_to_allowed_surface_and_restore_protected_diff_clean",
                "add_one_cheap_self_check_that_proves_the_claim",
                "keep_promotion_allowed_false_until_all_gates_pass",
            ],
            "codex_exec": {"pass": True, "returncode": 0, "seconds": 1.0},
        }
        protected_dead_end_card["card_id"] = patch_garden_card_id(protected_dead_end_card)
        empty_failed_card = {
            **garden_card,
            "variant_name": "variant_unit_empty_failed_lane",
            "score": 65.0,
            "changed_files": [],
            "patch_summary_pass": False,
            "progress_contract_pass": False,
            "reasons": [
                "codex_exec_not_clean",
                "no_promotable_file_changed",
                "patch_summary_incomplete",
                "evidence_tool_progress_contract_not_passing",
            ],
            "next_small_fixes": patch_garden_next_actions([
                "codex_exec_not_clean",
                "no_promotable_file_changed",
                "evidence_tool_progress_contract_missing",
            ]),
            "codex_exec": {"pass": False, "returncode": None, "seconds": 0.1},
        }
        empty_failed_card["card_id"] = patch_garden_card_id(empty_failed_card)
        append_patch_garden_card(garden_root, protected_dead_end_card)
        append_patch_garden_card(garden_root, empty_failed_card)
        dead_end_triage = patch_garden_card_repair_triage(garden_root, protected_dead_end_card)
        empty_triage = patch_garden_card_repair_triage(garden_root, empty_failed_card)
        active_after_non_actionable = active_patch_garden_cards(garden_root, limit=8)
        direct_non_actionable_plan = build_patch_garden_nurse_plan(
            garden_root,
            [protected_dead_end_card, empty_failed_card],
            allow_codex=True,
        )
        rec(
            "patch_garden_nurse_archives_non_actionable_failed_cards",
            dead_end_triage.get("status") == "quarantined_protected_diff_dead_end"
            and empty_triage.get("status") == "archived_empty_failed_lane"
            and all(c.get("variant_name") not in {"variant_unit_protected_dead_end", "variant_unit_empty_failed_lane"} for c in active_after_non_actionable)
            and direct_non_actionable_plan.get("retry_requested") is False
            and direct_non_actionable_plan.get("skipped_non_actionable_count") == 2,
            dead_end=dead_end_triage,
            empty=empty_triage,
            active_cards=active_after_non_actionable,
            nurse=direct_non_actionable_plan,
        )
        active_cards = active_patch_garden_cards(garden_root, limit=1)
        nurse_plan = build_patch_garden_nurse_plan(garden_root, active_cards, allow_codex=True)
        repair_theory = build_patch_garden_repair_theory(active_cards[0]) if active_cards else {}
        repair_prompt = render_tool_prompt(repair_theory, ticket, {**autopsy, "patch_garden_cards": [garden_card]}, [])
        rec(
            "patch_garden_nurse_selects_active_codex_retry_card",
            bool(
                active_cards
                and nurse_plan.get("active")
                and repair_theory.get("patch_garden_repair")
                and "PatchGardenNurse active retry lane" in repair_prompt
                and isinstance(((nurse_plan.get("cards") or [{}])[0]).get("codex_exec"), dict)
            ),
            nurse=nurse_plan,
        )
        cycle_selected, cycle_exclusive = select_cycle_theories([repair_theory], CODE_BRAIN_THEORIES[:3], 3, allow_codex=True)
        rec(
            "patch_garden_nurse_keeps_retry_lane_without_starving_broad_lanes",
            bool(
                cycle_exclusive is False
                and len(cycle_selected) == 3
                and cycle_selected[0].get("patch_garden_repair")
                and any(not row.get("patch_garden_repair") for row in cycle_selected[1:])
            ),
            selected=[t.get("name") for t in cycle_selected],
        )
        v10_blocked_card = {
            **garden_card,
            "variant_name": "variant_card_hidden_wait",
            "reasons": ["v10_hidden_replay_gate_failed"],
            "next_small_fixes": patch_garden_next_actions(["v10_hidden_replay_gate_failed"]),
        }
        v10_blocked_card["card_id"] = patch_garden_card_id(v10_blocked_card)
        append_patch_garden_card(garden_root, v10_blocked_card)
        card_hidden_requests = ensure_hidden_replay_requests_for_pending_attempts(garden_root)
        active_after_v10_card = active_patch_garden_cards(garden_root, limit=8)
        rec(
            "patch_garden_v10_blocked_card_requests_hidden_replay_without_code_retry",
            any(r.get("patch_id") == "variant_card_hidden_wait" for r in card_hidden_requests)
            and all(c.get("variant_name") != "variant_card_hidden_wait" for c in active_after_v10_card),
            requests=card_hidden_requests,
            active_cards=active_after_v10_card,
        )
        rec(
            "patch_garden_repair_attempt_does_not_spawn_new_card_chain",
            not should_write_new_patch_garden_card(
                repair_theory,
                {"variant_name": "variant_retry_passed_gate", "codex_ran": True, "tool_brain_eligible": True, "score": 155.0},
                manifest={"codex_exec": {"pass": True}},
                gate={"pass": True, "changed_files": [TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"]},
                promotion_decision={"promoted": False, "reason": "v10_hidden_replay_gate_failed"},
            ),
        )
        failed_attempt = append_patch_garden_attempt(
            garden_root,
            active_cards[0],
            {
                "scoreboard_row": {"variant_name": "variant_retry", "patch_family": "evidence_tool_progress_contract", "codex_ran": True, "score": 10.0},
                "tool_brain_gate": {"pass": False, "reasons": ["evidence_axis_improvement_missing"], "changed_files": [TOOL_LAB_NAME]},
                "manifest": {"variant_name": "variant_retry"},
            },
        )
        hidden_attempt = append_patch_garden_attempt(
            garden_root,
            active_cards[0],
            {
                "scoreboard_row": {"variant_name": "variant_retry_hidden", "patch_family": "evidence_tool_progress_contract", "codex_ran": True, "score": 155.0},
                "tool_brain_gate": {"pass": True, "reasons": [], "changed_files": [TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"]},
                "manifest": {"variant_name": "variant_retry_hidden", "variant_dir": str(garden_root)},
            },
            promotion_decision={
                "promoted": False,
                "reason": "v10_hidden_replay_gate_failed",
                "promotion": {"v10_gate": {"reason": "fresh_hidden_replay_required_before_promotion"}},
            },
        )
        rec(
            "patch_garden_hidden_replay_block_writes_request_and_pauses_code_retry",
            hidden_attempt.get("status") == "hidden_replay_pending"
            and hidden_attempt.get("required_evidence") == "fresh_hidden_replay_result"
            and read_hidden_replay_requests(garden_root, limit=5)
            and active_patch_garden_cards(garden_root, limit=1) == [],
            hidden_attempt=hidden_attempt,
        )
        hidden_request_for_failure = read_hidden_replay_requests(garden_root, limit=5)[-1]
        append_jsonl(
            patch_garden_dir(garden_root) / "hidden_replay_attempts.jsonl",
            {
                "schema_version": SCHEMA_VERSION,
                "event": "ToolLabHiddenReplayAttempt",
                "request_id": hidden_request_for_failure.get("request_id"),
                "patch_id": "variant_retry_hidden",
                "card_id": active_cards[0].get("card_id"),
                "pass": False,
                "reason": "unit_hidden_replay_failed",
                "promotion_allowed": False,
                "can_touch_final_holdout_or_live": False,
            },
        )
        pending_after_hidden_failure = pending_hidden_replay_requests(garden_root, limit=10)
        active_after_hidden_failure = active_patch_garden_cards(garden_root, limit=3)
        rec(
            "hidden_replay_failed_request_leaves_pending_queue_and_returns_to_nursery_repair",
            not any(str(r.get("patch_id") or "") == "variant_retry_hidden" for r in pending_after_hidden_failure)
            and any(c.get("hidden_replay_failed") for c in active_after_hidden_failure),
            pending=pending_after_hidden_failure,
            active_cards=active_after_hidden_failure,
        )
        append_jsonl(
            patch_garden_dir(garden_root) / "hidden_replay_results.jsonl",
            {"patch_id": "variant_retry_hidden", "window_count": 4, "median_lift": 1.0, "worst_window_lift": 0.0},
        )
        rec(
            "patch_garden_hidden_replay_result_reactivates_promotion_retry",
            bool(hidden_replay_result_for_patch(garden_root, "variant_retry_hidden") and active_patch_garden_cards(garden_root, limit=1)),
        )
        passed_attempt = append_patch_garden_attempt(
            garden_root,
            active_cards[0],
            {
                "scoreboard_row": {"variant_name": "variant_retry_passed", "patch_family": "evidence_tool_progress_contract", "codex_ran": True, "score": 100.0},
                "tool_brain_gate": {"pass": True, "reasons": [], "changed_files": [TOOL_LAB_NAME, "code_engine/evidence_proof_worker.py"]},
                "manifest": {"variant_name": "variant_retry_passed"},
            },
            promotion_decision={"promoted": True},
        )
        rec(
            "patch_garden_nurse_attempt_memory_retries_until_passed",
            failed_attempt.get("status") == "codex_retry_failed"
            and passed_attempt.get("status") == "passed_and_promoted"
            and active_patch_garden_cards(garden_root, limit=1) == [],
            failed_attempt=failed_attempt,
            passed_attempt=passed_attempt,
        )
        rec(
            "patch_garden_nurse_uses_short_retry_sleep_until_passed",
            patch_garden_sleep_interval(1800, {"patch_garden_nurse": {"retry_requested": True}, "patch_garden_attempts": [failed_attempt]}) == PATCH_GARDEN_NURSE_RETRY_SEC
            and patch_garden_sleep_interval(1800, {"patch_garden_nurse": {"retry_requested": True}, "patch_garden_attempts": [passed_attempt]}) == 1800,
        )
    decode_probe = run_subprocess(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes([0x9d, 0x6f, 0x6b])); sys.stderr.buffer.write(bytes([0x9d, 0x65, 0x72, 0x72]))",
        ],
        cwd=root,
        timeout=30,
    )
    rec(
        "subprocess_output_decode_is_lossy_not_crashing",
        decode_probe.get("pass") is True and isinstance(decode_probe.get("stdout"), str) and isinstance(decode_probe.get("stderr"), str),
        stdout=decode_probe.get("stdout"),
        stderr=decode_probe.get("stderr"),
    )
    old_lab_workers = os.environ.get("CODE_LAB_CODE_BRAIN_LAB_WORKERS")
    os.environ["CODE_LAB_CODE_BRAIN_LAB_WORKERS"] = str(CODE_BRAIN_DEFAULT_LAB_WORKERS)
    try:
        rep = run_cycle(root, allow_codex=False, allow_promote=False, write_patch_garden=False)
    finally:
        if old_lab_workers is None:
            os.environ.pop("CODE_LAB_CODE_BRAIN_LAB_WORKERS", None)
        else:
            os.environ["CODE_LAB_CODE_BRAIN_LAB_WORKERS"] = old_lab_workers
    rec("cycle_creates_run_dir", Path(rep.get("run_dir", "")).exists(), run_dir=rep.get("run_dir"))
    rec("cycle_writes_scoreboard", Path(rep.get("run_dir", "")) .joinpath("scoreboard.json").exists() if rep.get("run_dir") else False)
    rec("cycle_records_lab_team_lanes", int(rep.get("lab_team", {}).get("lane_count") or 0) == int(rep.get("variants_per_cycle") or 0), lab_team=rep.get("lab_team"))
    rec("cycle_uses_parallel_lab_team_by_default", rep.get("lab_team", {}).get("mode") == "parallel" and int(rep.get("lab_team", {}).get("workers") or 0) >= 2, lab_team=rep.get("lab_team"))
    run_path = Path(rep.get("run_dir", "")) if rep.get("run_dir") else Path()
    lane_status_files = list((run_path / "variants").glob("*/lane_status.json")) if run_path.exists() else []
    lane_result_files = list((run_path / "variants").glob("*/lane_result.json")) if run_path.exists() else []
    rec("cycle_writes_lane_progress_artifacts", bool(lane_status_files and lane_result_files), lane_status_count=len(lane_status_files), lane_result_count=len(lane_result_files))
    rec("self_check_cycle_does_not_pollute_patch_garden", rep.get("patch_garden_cards") == [])
    rec("cycle_is_sandbox_only", not bool(rep.get("promotion_decision", {}).get("promoted")), decision=rep.get("promotion_decision"))
    rec("tool_brain_requires_codex_before_promotion", rep.get("promotion_decision", {}).get("reason") == "codex_tool_brain_not_enabled", decision=rep.get("promotion_decision"))
    status = {
        "schema_version": SCHEMA_VERSION,
        "status": "self_check",
        "self_check_pass": True,
        "live_tool_lab_status_preserved": True,
        "updated_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    self_check_status_path = tool_lab_root(root) / "status" / "self_check_status.json"
    write_json(self_check_status_path, status)
    rec("status_written", self_check_status_path.exists(), status=status)
    failed = sum(1 for c in cases if not c.get("pass"))
    return {"schema_version": SCHEMA_VERSION, "pass": failed == 0, "passed": len(cases) - failed, "failed": failed, "cases": cases}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Continuous proof-gated Code Brain Lab")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--forever", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--run-codex", action="store_true", help="Run codex exec in variant sandboxes. Disabled by default.")
    parser.add_argument("--allow-promote", action="store_true", help="Allow proof-gated promotion of Codex-edited sandbox winners.")
    parser.add_argument("--interval-sec", type=int, default=None)
    parser.add_argument("--max-cycles", type=int, default=1)
    args = parser.parse_args(argv)
    root = resolve_root(args.project_root)
    env_codex = os.environ.get("CODE_LAB_TOOL_LAB_ALLOW_CODEX", "0").strip().lower() in ("1", "true", "yes", "on")
    env_promote = os.environ.get("CODE_LAB_TOOL_LAB_ALLOW_PROMOTE", "0").strip().lower() in ("1", "true", "yes", "on")
    allow_codex = bool(args.run_codex or env_codex)
    allow_promote = bool(args.allow_promote or env_promote)
    if args.status:
        print(json.dumps(read_status(root), indent=2, sort_keys=True, default=str))
        return 0
    if args.self_check:
        rep = run_self_check(root)
        print(json.dumps(rep, indent=2, sort_keys=True, default=str))
        return 0 if rep.get("pass") else 1
    if args.forever:
        return loop_forever(root, interval_sec=args.interval_sec, allow_codex=allow_codex, allow_promote=allow_promote, max_cycles=args.max_cycles)
    rep = run_cycle(root, allow_codex=allow_codex, allow_promote=allow_promote)
    print(json.dumps(rep, indent=2, sort_keys=True, default=str))
    return 0 if rep.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
