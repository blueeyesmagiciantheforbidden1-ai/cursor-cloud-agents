"""Finite-model evidence only: these checks do not execute a distributed system."""
import copy
import unittest
from unittest.mock import patch

from agent_hub.workflows import (BudgetExhausted, CheckCache, DEFAULT_PROPERTIES, ResearchBudget,
                                 WorkflowError, apply_edits, migrate_states, reference_check,
                                 synthesize, validate_model, verify_migration)


def state(identifier, phase, *, expired=False, request=False, response=False):
    return {"id": identifier, "labels": {"phase": phase, "lease_expired": expired,
                                        "request": request, "response": response}}


def edge(identifier, source, target, *, accepts=False):
    return {"id": identifier, "source": source, "action": identifier,
            "target": target, "accepts_completion": accepts}


def good_model():
    return {"id": "one-job", "states": [state("pending", "pending", request=True), state("running", "running"),
                                         state("done", "completed", response=True)],
            "initial": ["pending"], "transitions": [edge("start", "pending", "running"),
                                                     edge("commit", "running", "done", accepts=True)]}


def bad_retry_model():
    model = good_model()
    model["transitions"].append(edge("retry", "done", "pending"))
    return model


def response_property(within=2):
    return {"id": "respond", "kind": "bounded_response", "trigger": {"request": True},
            "response": {"response": True}, "within": within}


class ReferenceTests(unittest.TestCase):
    def test_safe_finite_model_is_verified_only_through_requested_horizon(self):
        result = reference_check(good_model(), [*DEFAULT_PROPERTIES, response_property()], horizon=5)
        self.assertEqual(result["status"], "bounded_verified")
        self.assertEqual(result["horizon"], 5)
        self.assertTrue(result["exhaustive_within_horizon"])
        self.assertEqual(result["reduction"], "none")
        self.assertGreater(result["checked_states"], 0)

    def test_completed_job_returning_to_pending_has_short_counterexample(self):
        result = reference_check(bad_retry_model(), horizon=5)
        self.assertEqual(result["status"], "counterexample")
        self.assertEqual(result["counterexample"]["kind"], "completed_never_pending")
        self.assertEqual([item["state"] for item in result["counterexample"]["trace"]],
                         ["pending", "running", "done", "pending"])
        self.assertFalse(result["exhaustive_within_horizon"])

    def test_history_monitor_prevents_unsound_merging_of_completed_and_fresh_paths(self):
        model = {"id": "history-sensitive", "states": [state("a-fresh", "pending"), state("z-done", "completed"),
                                                          state("common", "running"), state("pending", "pending")],
                 "initial": ["a-fresh", "z-done"], "transitions": [edge("fresh-to-common", "a-fresh", "common"),
                        edge("done-to-common", "z-done", "common"), edge("retry", "common", "pending")]}
        result = reference_check(model, [DEFAULT_PROPERTIES[0]], horizon=2)
        self.assertEqual(result["counterexample"]["trace"][0]["state"], "z-done")

    def test_expired_lease_cannot_accept_a_completion(self):
        model = good_model()
        model["states"][1]["labels"]["lease_expired"] = True
        result = reference_check(model, horizon=3)
        self.assertEqual(result["counterexample"]["kind"], "expired_cannot_commit")
        last = result["counterexample"]["trace"][-1]
        self.assertTrue(last["via"]["accepts_completion"])
        self.assertEqual(last["via"]["source"], "running")

    def test_duplicate_logical_acceptance_is_counted_across_reconverging_paths(self):
        model = good_model()
        model["transitions"].append(edge("first-accept", "pending", "running", accepts=True))
        result = reference_check(model, [DEFAULT_PROPERTIES[2]], horizon=3)
        self.assertEqual(result["counterexample"]["kind"], "at_most_one_completion")
        self.assertEqual(sum(bool(item["via"] and item["via"]["accepts_completion"])
                             for item in result["counterexample"]["trace"]), 2)

    def test_response_bound_is_inclusive_and_incomplete_horizon_is_not_a_proof(self):
        properties = [response_property(within=2)]
        self.assertEqual(reference_check(good_model(), properties, horizon=2)["status"], "bounded_verified")
        self.assertEqual(reference_check(good_model(), properties, horizon=1)["status"], "horizon_inconclusive")
        late = reference_check(good_model(), [response_property(within=1)], horizon=2)
        self.assertEqual(late["status"], "counterexample")
        self.assertEqual(late["counterexample"]["trace"][-1]["step"], 1)

    def test_deadlock_stutters_and_cannot_hide_unanswered_requests(self):
        model = good_model()
        model["transitions"] = []
        result = reference_check(model, [response_property()], horizon=3)
        self.assertEqual(result["status"], "counterexample")
        self.assertEqual(result["counterexample"]["trace"][-1]["via"]["id"], "deadlock-stutter")

    def test_every_nondeterministic_path_must_meet_response_requirement(self):
        model = good_model()
        model["transitions"].append(edge("wait", "running", "running"))
        result = reference_check(model, [response_property()], horizon=3)
        self.assertEqual(result["status"], "counterexample")
        self.assertEqual(result["counterexample"]["trace"][-1]["via"]["id"], "wait")

    def test_zero_step_response_and_zero_horizon_are_explicit(self):
        result = reference_check(good_model(), [response_property(0)], horizon=0)
        self.assertEqual(result["status"], "counterexample")
        self.assertEqual(len(result["counterexample"]["trace"]), 1)
        model = good_model()
        model["states"][0]["labels"]["response"] = True
        self.assertEqual(reference_check(model, [response_property(0)], horizon=0)["status"], "bounded_verified")

    def test_exhausted_check_budget_never_returns_verified(self):
        for budget in (ResearchBudget(max_check_states=0), ResearchBudget(max_check_states=2),
                       ResearchBudget(max_transitions=0)):
            result = reference_check(good_model(), horizon=10, budget=budget)
            self.assertEqual(result["status"], "budget_exhausted")
            self.assertFalse(result["exhaustive_within_horizon"])
            self.assertLessEqual(budget.check_states, budget.max_check_states)
            self.assertLessEqual(budget.transitions, budget.max_transitions)

    def test_invalid_unbounded_or_executable_dsl_is_rejected(self):
        invalid = good_model()
        invalid["transitions"][0]["guard"] = "eval(arbitrary_code)"
        with self.assertRaises(WorkflowError):
            validate_model(invalid)
        for horizon in (-1, 65, True):
            with self.assertRaises(WorkflowError):
                reference_check(good_model(), horizon=horizon)
        invalid = good_model()
        invalid["states"][1]["labels"]["request"] = 1
        with self.assertRaises(WorkflowError):
            validate_model(invalid)


class CacheTests(unittest.TestCase):
    def test_only_exact_model_properties_horizon_and_dependency_hashes_reuse_evidence(self):
        cache = CheckCache()
        properties = [response_property()]
        dependencies = {"verifier-data": "a" * 64}
        first = reference_check(good_model(), properties, horizon=4, dependencies=dependencies, cache=cache)
        budget = ResearchBudget(max_check_states=0)
        hit = reference_check(good_model(), properties, horizon=4, dependencies=dependencies, cache=cache, budget=budget)
        self.assertTrue(hit["cache_hit"])
        self.assertEqual(hit["checked_states"], 0)
        self.assertEqual(hit["verification_work"], first["verification_work"])
        self.assertEqual(budget.cache_hits, 1)
        changed_model = good_model()
        changed_model["states"].append(state("unreachable", "cancelled"))
        variants = ((changed_model, properties, 4, dependencies), (good_model(), [response_property(3)], 4, dependencies),
                    (good_model(), properties, 3, dependencies), (good_model(), properties, 4, {"verifier-data": "b" * 64}))
        for model, props, horizon, deps in variants:
            result = reference_check(model, props, horizon=horizon, dependencies=deps, cache=cache)
            self.assertFalse(result["cache_hit"])
            self.assertNotEqual(result["cache_key"], first["cache_key"])
        with patch("agent_hub.workflows.CHECKER_VERSION", "different-semantics"):
            self.assertFalse(reference_check(good_model(), properties, horizon=4, dependencies=dependencies, cache=cache)["cache_hit"])

    def test_budget_exhaustion_is_not_cached_and_returns_are_defensive_copies(self):
        cache = CheckCache(max_entries=1)
        result = reference_check(good_model(), budget=ResearchBudget(max_check_states=0), cache=cache)
        self.assertEqual(result["status"], "budget_exhausted")
        valid = reference_check(good_model(), cache=cache)
        self.assertFalse(valid["cache_hit"])
        valid["status"] = "tampered"
        self.assertEqual(reference_check(good_model(), cache=cache)["status"], "bounded_verified")
        reference_check(bad_retry_model(), cache=cache)
        self.assertFalse(reference_check(good_model(), cache=cache)["cache_hit"])


class MigrationTests(unittest.TestCase):
    def test_bijective_rename_preserves_all_behavior_labels_and_initial_states(self):
        original = bad_retry_model()
        mapping = {"pending": "state-c", "running": "state-a", "done": "state-b"}
        budget = ResearchBudget()
        migration = migrate_states(original, mapping, budget=budget)
        self.assertTrue(migration["evidence"]["equivalent"])
        self.assertEqual(budget.check_states, 3)
        self.assertEqual(budget.transitions, 3)
        before = reference_check(original, horizon=4)
        after = reference_check(migration["model"], horizon=4)
        self.assertEqual(before["status"], after["status"])
        self.assertEqual([mapping[item["state"]] for item in before["counterexample"]["trace"]],
                         [item["state"] for item in after["counterexample"]["trace"]])

    def test_migration_rejects_nonbijective_and_semantics_changing_mappings(self):
        original = good_model()
        mapping = {identifier: "new-" + identifier for identifier in ("pending", "running", "done")}
        migrated = migrate_states(original, mapping)["model"]
        for change in ("initial", "labels", "edge", "effect"):
            edited = copy.deepcopy(migrated)
            if change == "initial":
                edited["initial"] = ["new-done"]
            elif change == "labels":
                edited["states"][0]["labels"]["phase"] = "cancelled"
            elif change == "edge":
                edited["transitions"][0]["target"] = "new-pending"
            else:
                edited["transitions"][0]["accepts_completion"] = False
            self.assertFalse(verify_migration(original, edited, mapping)["equivalent"], change)
        with self.assertRaises(WorkflowError):
            migrate_states(original, {"pending": "same", "running": "same", "done": "other"})
        with self.assertRaises(BudgetExhausted):
            migrate_states(original, mapping, budget=ResearchBudget(max_check_states=1))

    def test_boolean_and_integer_label_encodings_are_not_equivalent(self):
        original = good_model()
        mapping = {identifier: identifier for identifier in ("pending", "running", "done")}
        changed = copy.deepcopy(original)
        for item in changed["states"]:
            item["labels"]["request"] = int(item["labels"]["request"])
        self.assertFalse(verify_migration(original, changed, mapping)["equivalent"])


class SynthesisTests(unittest.TestCase):
    def test_simple_repair_has_counterexample_and_fresh_bounded_verification_evidence(self):
        model = bad_retry_model()
        allowed = [{"kind": "remove_transition", "transition_id": "aaa-missing"},
                   {"kind": "remove_transition", "transition_id": "retry"}]
        properties = [*DEFAULT_PROPERTIES, response_property()]
        budget = ResearchBudget(max_candidates=4, max_check_states=100, max_transitions=100)
        result = synthesize(model, allowed, properties, horizon=4, budget=budget)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["edits"], [allowed[1]])
        self.assertEqual([item["status"] for item in result["attempts"]], ["counterexample", "invalid_candidate", "bounded_verified"])
        self.assertEqual(result["budget"]["candidates"], 3)
        self.assertEqual(result["budget"]["check_states"], sum(item["checked_states"] for item in result["attempts"]))
        self.assertGreater(result["attempts"][0]["checked_states"], 0)
        self.assertEqual(reference_check(result["candidate"], properties, horizon=4)["status"], "bounded_verified")
        self.assertEqual(model, bad_retry_model(), "Candidate edits must not change the source")

    def test_candidate_and_checker_budgets_stop_search_including_failures(self):
        allowed = [{"kind": "remove_transition", "transition_id": "aaa-missing"},
                   {"kind": "remove_transition", "transition_id": "retry"}]
        result = synthesize(bad_retry_model(), allowed, horizon=4, budget=ResearchBudget(max_candidates=2))
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["budget"]["candidates"], 2)
        self.assertEqual(len(result["attempts"]), 2)
        self.assertIsNone(result["candidate"])
        result = synthesize(bad_retry_model(), allowed, horizon=4, budget=ResearchBudget(max_check_states=1))
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["budget"]["candidates"], 1)
        self.assertEqual(result["budget"]["check_states"], 1)

    def test_search_is_deterministic_and_cache_hits_still_count_candidate_attempts(self):
        allowed = [{"kind": "remove_transition", "transition_id": "retry"}]
        cache = CheckCache()
        first = synthesize(bad_retry_model(), allowed, horizon=4, cache=cache)
        second = synthesize(bad_retry_model(), allowed[::-1], horizon=4, cache=cache)
        self.assertEqual(first["candidate"], second["candidate"])
        self.assertEqual(first["edits"], second["edits"])
        self.assertEqual(first["budget"]["candidates"], second["budget"]["candidates"])
        self.assertEqual(second["budget"]["check_states"], 0)
        self.assertEqual(second["budget"]["cache_hits"], 2)

    def test_removing_all_progress_does_not_pass_bounded_response(self):
        result = synthesize(bad_retry_model(), [{"kind": "remove_transition", "transition_id": "start"}],
                            [*DEFAULT_PROPERTIES, response_property()], horizon=4)
        self.assertEqual(result["status"], "no_repair")
        self.assertEqual(result["attempts"][-1]["counterexample"]["kind"], "bounded_response")

    def test_candidates_cannot_rewrite_state_labels_requirements_or_initial_state(self):
        for kind in ("edit_labels", "replace_checker", "change_initial", "change_requirements"):
            with self.assertRaises(WorkflowError):
                synthesize(good_model(), [{"kind": kind, "transition_id": "start"}])
        with self.assertRaises(WorkflowError):
            apply_edits(good_model(), [{"kind": "remove_transition", "transition_id": "start"},
                                        {"kind": "redirect_transition", "transition_id": "start", "target": "done"}])


if __name__ == "__main__":
    unittest.main()
