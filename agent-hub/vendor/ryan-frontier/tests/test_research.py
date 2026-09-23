"""Tests protect the experimental boundary, not just implementation details."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ryan_frontier import domain
from ryan_frontier import research as r


class _OpaqueTask:
    """Search has a public buggy program but no access to the answer."""
    def __init__(self, identifier, split="development"):
        self.task_id = identifier
        self.split = split

    @property
    def initial_candidate(self):
        return domain.candidate_spec(0)

    @property
    def required_guards(self):
        raise AssertionError("learner read hidden required guards")

    @property
    def scenarios(self):
        raise AssertionError("learner read hidden scenario metadata")


class _OpaqueVerification:
    def __init__(self, missing):
        self.passed = not missing
        self.failure_kind = ("opaque-failure-" + str(missing[0])) if missing else None
        self.counterexample = [{"opaque_trace": True}] if missing else []
        self.checked_states = 1 if missing else 7
        self.checked_transitions = 2 if missing else 19
        self.checked_policy_decisions = 1 if missing else 9
        self.acceptance_eligible = self.passed

    def to_dict(self):
        return {"counterexample": self.counterexample}


def _opaque_verify(task, candidate):
    del task
    mask = domain.candidate_mask(candidate)
    return _OpaqueVerification([i for i in (1, 3) if not mask & (1 << i)])


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = r.run_pilot(seed=719, units=3, budget=6,
                                 development_tasks=2, selection_tasks=2,
                                 confirmation_tasks=2)

    def test_method_learns_opaque_counterexample_repairs_without_oracle(self):
        with patch.object(domain, "verify", side_effect=_opaque_verify):
            learned = r.learn_method([_OpaqueTask("dev-a")], [_OpaqueTask("dev-b")], 3)
            evidence = learned["discovery"]["failure_rules"]
            self.assertEqual(dict(evidence), {"opaque-failure-1": 1, "opaque-failure-3": 3})
            method = r.ResearchMethod("opaque-policy", "counterexample", failure_rules=evidence)
            out = r.run_search(_OpaqueTask("fresh", "confirmation"), method, r.Capability(), 3)
            self.assertTrue(out["solved"])
            self.assertEqual([row["mask"] for row in out["observations"]], [0, 2, 10])
            self.assertEqual(out["cost"]["failed_candidates"], 2)

    def test_learning_rejects_confirmation_and_overlapping_tasks(self):
        with self.assertRaisesRegex(ValueError, "development tasks only"):
            r.learn_method([_OpaqueTask("a", "confirmation")], [_OpaqueTask("b")], 3)
        with self.assertRaisesRegex(ValueError, "disjoint"):
            r.learn_method([_OpaqueTask("same")], [_OpaqueTask("same")], 3)

    def test_freeze_precedes_confirmation_generation(self):
        events = []
        make = domain.make_tasks
        compile_method = r.compile_method
        def tracked_make(seed, count, split="development"):
            events.append(("make", split))
            return make(seed, count, split=split)
        def tracked_compile(method):
            events.append(("freeze", method.digest))
            return compile_method(method)
        with patch.object(domain, "make_tasks", side_effect=tracked_make), \
             patch.object(r, "compile_method", side_effect=tracked_compile):
            r.run_pilot(seed=43, units=1, budget=3, development_tasks=1,
                        selection_tasks=1, confirmation_tasks=1)
        freeze_index = next(i for i, e in enumerate(events) if e[0] == "freeze")
        for i, event in enumerate(events):
            if event[0] == "make" and event[1] in ("confirmation", "shift"):
                self.assertGreater(i, freeze_index)

    def test_heldout_data_cannot_change_frozen_method(self):
        original = domain.make_tasks
        def altered(seed, count, split="development"):
            return original(seed if split == "development" else seed + 909,
                            count, split=split)
        args = dict(seed=54, units=1, budget=4, development_tasks=1,
                    selection_tasks=1, confirmation_tasks=1)
        before = r.run_pilot(**args)
        with patch.object(domain, "make_tasks", side_effect=altered):
            after = r.run_pilot(**args)
        self.assertEqual(before["frozen_methods"], after["frozen_methods"])
        self.assertEqual(before["development"], after["development"])

    def test_seed_and_task_identity_disjointness(self):
        seeds = [value for row in self.report["seed_manifest"]
                 for key, value in row.items() if key != "lineage"]
        self.assertEqual(len(seeds), len(set(seeds)))
        development = {identifier for row in self.report["development"]["lineages"]
                       for identifier in row["task_ids"] + row["selection_task_ids"]}
        heldout = set()
        for unit in self.report["factorial"]["units"]:
            for condition in unit["conditions"].values():
                task_lists = [[t["task_id"] for t in arm["tasks"]]
                              for arm in condition["arms"].values()]
                self.assertTrue(all(ids == task_lists[0] for ids in task_lists))
                self.assertTrue(heldout.isdisjoint(task_lists[0]))
                heldout.update(task_lists[0])
        self.assertTrue(development.isdisjoint(heldout))

    def test_equal_exact_budgets_and_failed_work_are_counted(self):
        budget = self.report["design"]["candidate_budget_per_task_arm"]
        for unit in self.report["factorial"]["units"]:
            for condition in unit["conditions"].values():
                for arm in condition["arms"].values():
                    self.assertEqual(arm["cost"]["verifier_calls"], budget * len(arm["tasks"]))
                    for task in arm["tasks"]:
                        rows = task["observations"]
                        self.assertEqual(len(rows), budget)
                        self.assertEqual(len(set(row["mask"] for row in rows)), budget)
                        self.assertEqual(task["cost"]["failed_candidates"], sum(not row["passed"] for row in rows))
                        self.assertEqual(task["cost"]["checked_transitions"], sum(row["checked_transitions"] for row in rows))
                        self.assertEqual(task["cost"]["checked_states"], sum(row["checked_states"] for row in rows))
        self.assertGreater(self.report["lifecycle_cost"]["failed_candidates"], 0)

    def test_no_early_stop_even_after_success(self):
        task = domain.Task("easy", "confirmation", 6, 2, 2, r.GUARDS, initial_mask=31)
        result = r.run_search(task, r.ORIGINAL_METHOD, r.Capability(), 7)
        self.assertEqual(result["first_success_slot"], 1)
        self.assertEqual(result["cost"]["verifier_calls"], 7)

    def test_vacuous_verification_cannot_open_memory_gate_or_count_as_solved(self):
        task = domain.Task("vacuous", "development", 0, 1, 1, ())
        method = r.ResearchMethod("nonvacuous-only", "cumulative", capability_admission="after_success")
        result = r.run_search(task, method, r.Capability((31,)), 4)
        self.assertFalse(result["solved"])
        self.assertIsNone(result["selected_guard_count"])
        self.assertEqual(result["capability_admission"]["eligible_slots"], 0)
        self.assertEqual(result["cost"]["checked_policy_decisions"], 0)
        self.assertTrue(all(row["bounded_verification_passed"] for row in result["observations"]))
        self.assertTrue(all(not row["passed"] for row in result["observations"]))

    def test_complete_guard_baseline_exposes_the_domain_ceiling(self):
        for condition in self.report["fixed_baseline"]["summary"].values():
            self.assertEqual(condition["mean_solved_fraction"], 1.0)
        for unit in self.report["factorial"]["units"]:
            for condition in unit["conditions"].values():
                baseline = condition["fixed_baseline"]
                self.assertEqual(baseline["cost"]["verifier_calls"], 12)
                for task in baseline["tasks"]:
                    self.assertEqual(task["first_success_slot"], 1)
                    self.assertEqual(task["observations"][0]["mask"], 31)
        self.assertTrue(any("fixed solution" in note for note in self.report["limits"]))

    def test_discovery_and_all_method_candidates_are_in_cost(self):
        design = self.report["design"]
        expected = (32 * design["development_tasks_per_lineage"] +
                    r.METHOD_FAMILY_SIZE * design["selection_tasks_per_lineage"] *
                    design["candidate_budget_per_task_arm"])
        for row in self.report["development"]["lineages"]:
            self.assertEqual(row["cost"]["verifier_calls"], expected)
        self.assertEqual(self.report["development"]["total_cost"]["verifier_calls"], expected * 3)
        for key in self.report["lifecycle_cost"]:
            self.assertEqual(self.report["lifecycle_cost"][key],
                             self.report["development"]["total_cost"][key] +
                             self.report["confirmation"]["total_cost"][key])

    def test_frozen_policy_and_source_are_executable_and_deterministic(self):
        frozen = self.report["frozen_methods"][0]
        method = r.ResearchMethod.from_dict(frozen["spec"])
        namespace = {}
        source = self.report["artifacts"][frozen["artifact_id"]]["source"]
        exec(compile(source, "frozen-policy.py", "exec"), namespace)
        observations = []
        for _ in range(32):
            expected = r.propose(method, 0, observations, r.Capability((10,)), total_budget=32)
            self.assertEqual(namespace["propose"](0, observations, (10,), total_budget=32), expected)
            observations.append({"mask": expected, "passed": False, "failure_kind": "unseen"})
        self.assertEqual(len({o["mask"] for o in observations}), 32)
        with self.assertRaises(FrozenInstanceError):
            method.name = "mutated"

    def test_exported_policy_matches_interpreter_with_real_counterexamples(self):
        for frozen in self.report["frozen_methods"]:
            method = r.ResearchMethod.from_dict(frozen["spec"])
            capability = r.Capability(tuple(frozen["capability"]["macro_masks"]))
            namespace = {}
            exec(compile(r.compile_method(method), "frozen-policy.py", "exec"), namespace)
            for task in domain.make_tasks(224, 2, split="shift"):
                result = r.run_search(task, method, capability, 6)
                observations = []
                base = domain.candidate_mask(task.initial_candidate)
                for row in result["observations"]:
                    self.assertEqual(namespace["propose"](base, observations, capability.macro_masks,
                                                          total_budget=6), row["mask"])
                    observations.append(row)

    def test_deterministic_complete_pilot(self):
        again = r.run_pilot(seed=719, units=3, budget=6,
                            development_tasks=2, selection_tasks=2, confirmation_tasks=2)
        self.assertEqual(again, self.report)
        json.loads(json.dumps(self.report, allow_nan=False))

    def test_hoeffding_uses_difference_range_and_multiplicity(self):
        values = [0.25, 0.5, -0.25, 1.0]
        out = r.paired_hoeffding(values, alpha=.05, candidates=4, conditions=2, comparisons=5)
        expected = math.sqrt(2 * math.log(2 * 4 * 2 * 5 / .05) / 4)
        self.assertAlmostEqual(out["radius"], expected)
        self.assertEqual(out["multiplicity"], 40)
        self.assertAlmostEqual(out["mean"], .375)
        self.assertEqual(out["n_independent_lineages"], 4)
        self.assertEqual((out["lower"], out["upper"]), (-1, 1))
        single = r.paired_hoeffding(values)
        self.assertGreater(out["radius"], single["radius"])

    def test_inference_unit_is_lineage_not_task_or_check(self):
        for conditions in self.report["confirmation"]["intervals"].values():
            for interval in conditions.values():
                self.assertEqual(interval["n_independent_lineages"], 3)
                self.assertEqual(interval["multiplicity"], 40)
        self.assertEqual(self.report["confirmation"]["decision"], "inconclusive")
        self.assertFalse(self.report["confirmation"]["promoted"])

    def test_factorial_separates_policy_and_macro_memory(self):
        values = {"M0_C0": .1, "M0_C1": .4, "M1_C0": .5, "M1_C1": .9}
        contrasts = r._contrast(values)
        self.assertAlmostEqual(contrasts["method_without_memory"], .4)
        self.assertAlmostEqual(contrasts["memory_without_method"], .3)
        self.assertAlmostEqual(contrasts["method_with_memory"], .5)
        self.assertAlmostEqual(contrasts["half_interaction"], .05)

    def test_revised_family_gates_memory_and_original_control_is_unchanged(self):
        self.assertEqual(r.ORIGINAL_METHOD.capability_admission, "memory_first")
        self.assertTrue(all(item["spec"]["capability_admission"] == "after_success"
                            for item in self.report["frozen_methods"]))
        self.assertTrue(all(item["spec"]["final_slot_fallback_mask"] == 31
                            for item in self.report["frozen_methods"]))
        self.assertIsNone(r.ORIGINAL_METHOD.final_slot_fallback_mask)
        for lineage in self.report["lineage_archive"]:
            self.assertTrue(all(row["method"]["capability_admission"] == "after_success"
                                for row in lineage["candidates"]))
        old_spec = r.ORIGINAL_METHOD.to_dict()
        del old_spec["capability_admission"]
        self.assertEqual(r.ResearchMethod.from_dict(old_spec).capability_admission, "memory_first")
        with self.assertRaisesRegex(ValueError, "admission"):
            r.ResearchMethod("invalid", capability_admission="optimistic")

    def test_memory_displacement_regression_is_fixed_without_extra_checks(self):
        task = domain.Task("development-composition-regression", "development", 6, 2, 2, r.GUARDS)
        unsafe = r.ResearchMethod("old-composition", "cumulative")
        safe = r.ResearchMethod("new-composition", "cumulative", capability_admission="after_success")
        memory = r.Capability((2, 8))
        old = r.run_search(task, unsafe, memory, 6)
        new = r.run_search(task, safe, memory, 6)
        standalone = r.run_search(task, safe, r.Capability(), 6)
        self.assertFalse(old["solved"])
        self.assertTrue(new["solved"])
        self.assertEqual(new["first_success_slot"], standalone["first_success_slot"])
        self.assertEqual([x["mask"] for x in new["observations"]],
                         [x["mask"] for x in standalone["observations"]])
        self.assertEqual(new["capability_admission"]["eligible_slots"], 0)
        self.assertEqual(new["capability_admission"]["deferred_slots"], 6)
        self.assertEqual(old["cost"]["verifier_calls"], new["cost"]["verifier_calls"])

    def test_memory_is_admitted_adaptively_after_success_and_can_simplify(self):
        method = r.ResearchMethod("success-gated", "cumulative", capability_admission="after_success")
        def only_fence(task, candidate):
            return _OpaqueVerification([] if domain.candidate_mask(candidate) & 2 else [1])
        with patch.object(domain, "verify", side_effect=only_fence):
            plain = r.run_search(_OpaqueTask("secondary-a"), method, r.Capability(), 4)
            memory = r.run_search(_OpaqueTask("secondary-b"), method, r.Capability((2,)), 4)
        self.assertEqual([row["mask"] for row in plain["observations"]], [0, 1, 3, 7])
        self.assertEqual([row["mask"] for row in memory["observations"]], [0, 1, 3, 2])
        self.assertEqual([row["memory_eligible"] for row in memory["observations"]], [False, False, False, True])
        self.assertEqual(plain["first_success_slot"], memory["first_success_slot"])
        self.assertEqual(plain["selected_guard_count"], 2)
        self.assertEqual(memory["selected_guard_count"], 1)
        self.assertEqual(memory["capability_admission"]["eligible_slots"], 1)
        self.assertEqual(memory["cost"]["verifier_calls"], 4)

    def test_success_prefix_guarantee_across_budgets_and_exported_policy(self):
        tasks = domain.make_tasks(3344, 4, split="development")
        memory = r.Capability((2, 8))
        for strategy in ("breadth", "cumulative", "counterexample"):
            method = r.ResearchMethod("prefix-proof-test", strategy,
                                      failure_rules=tuple((g, i) for i, g in enumerate(r.GUARDS)),
                                      capability_admission="after_success")
            namespace = {}
            exec(compile(r.compile_method(method), "transferred.py", "exec"), namespace)
            for task in tasks:
                for budget in (1, 3, 6, 8):
                    plain = r.run_search(task, method, r.Capability(), budget)
                    composed = r.run_search(task, method, memory, budget)
                    self.assertEqual(plain["solved"], composed["solved"])
                    self.assertEqual(plain["first_success_slot"], composed["first_success_slot"])
                    prefix = plain["first_success_slot"] or budget
                    self.assertEqual([x["mask"] for x in plain["observations"][:prefix]],
                                     [x["mask"] for x in composed["observations"][:prefix]])
                    observations = []
                    for expected in composed["observations"]:
                        mask = namespace["propose"](task.initial_mask, observations, memory.macro_masks)
                        self.assertEqual(mask, expected["mask"])
                        # Execute the transferred program and real verifier,
                        # rather than merely feeding it an expected transcript.
                        verified = domain.verify(task, domain.candidate_spec(mask))
                        self.assertEqual(verified.acceptance_eligible, expected["passed"])
                        observations.append({"mask": mask, "passed": verified.acceptance_eligible,
                                             "failure_kind": verified.failure_kind})

    def test_report_guarantee_is_primary_only_and_secondary_is_conditional(self):
        self.assertEqual(self.report["design"]["capability_composition"]["original_control"], "memory_first")
        self.assertIn("lower primitive verification work", self.report["capability_composition_guarantee"]["not_guaranteed"])
        for unit in self.report["factorial"]["units"]:
            for condition in unit["conditions"].values():
                plain = condition["arms"]["M1_C0"]["tasks"]
                memory = condition["arms"]["M1_C1"]["tasks"]
                self.assertEqual([t["solved"] for t in plain], [t["solved"] for t in memory])
                self.assertEqual([t["first_success_slot"] for t in plain], [t["first_success_slot"] for t in memory])
                for row in plain + memory:
                    expected = row["selected_mask"].bit_count() if row["solved"] else None
                    self.assertEqual(row["selected_guard_count"], expected)

    def test_final_slot_fallback_repairs_under_tiny_budgets_without_extra_checks(self):
        task = domain.Task("development-fallback-budget", "development", 6, 2, 2, r.GUARDS)
        method = r.ResearchMethod("complete-bounded-domain", "breadth",
                                  capability_admission="after_success", final_slot_fallback_mask=31)
        namespace = {}
        exec(compile(r.compile_method(method), "fallback-transfer.py", "exec"), namespace)
        for budget in (1, 2, 3, 6, 32):
            result = r.run_search(task, method, r.Capability((2, 8)), budget)
            self.assertTrue(result["solved"])
            self.assertEqual(result["cost"]["verifier_calls"], budget)
            self.assertEqual(len({row["mask"] for row in result["observations"]}), budget)
            self.assertLessEqual(result["completion_fallback"]["invocations"], 1)
            self.assertTrue(result["completion_fallback"]["accepted"])
            self.assertTrue(result["observations"][-1]["fallback_invoked"])
            self.assertEqual(result["observations"][-1]["mask"], 31)
            observations = []
            for expected in result["observations"]:
                mask = namespace["propose"](0, observations, (2, 8), total_budget=budget)
                self.assertEqual(mask, expected["mask"])
                verified = domain.verify(task, domain.candidate_spec(mask))
                observations.append({"mask": mask, "passed": verified.acceptance_eligible,
                                     "failure_kind": verified.failure_kind})

    def test_fallback_does_not_replace_successful_incumbent_or_hide_vacuity(self):
        method = r.ResearchMethod("bounded-completion", "cumulative",
                                  capability_admission="after_success", final_slot_fallback_mask=31)
        easy = domain.Task("fallback-incumbent", "development", 6, 2, 2, r.GUARDS, initial_mask=31)
        accepted = r.run_search(easy, method, r.Capability((2,)), 3)
        self.assertEqual(accepted["first_success_slot"], 1)
        self.assertEqual(accepted["completion_fallback"]["invocations"], 0)
        vacuous = domain.Task("fallback-vacuous", "development", 0, 1, 1, ())
        rejected = r.run_search(vacuous, method, r.Capability(), 2)
        self.assertFalse(rejected["solved"])
        self.assertEqual(rejected["completion_fallback"]["invocations"], 1)
        self.assertFalse(rejected["completion_fallback"]["accepted"])

    def test_fallback_contract_fails_closed_and_never_rechecks_seen_mask(self):
        method = r.ResearchMethod("contract", final_slot_fallback_mask=31)
        with self.assertRaisesRegex(ValueError, "total_budget"):
            r.propose(method, 0, [])
        for budget in (0, 33, True):
            with self.assertRaises(ValueError):
                r.propose(method, 0, [], total_budget=budget)
        with self.assertRaisesRegex(ValueError, "exhausted"):
            r.propose(method, 0, [{"mask": 0, "passed": False}], total_budget=1)
        with self.assertRaisesRegex(ValueError, "mask 31"):
            r.ResearchMethod("unproved-fallback", final_slot_fallback_mask=7)
        # The condition for completeness is false for this synthetic history;
        # do not conceal the failure by re-verifying an already rejected mask.
        self.assertNotEqual(r.propose(method, 0, [{"mask": 31, "passed": False}], total_budget=2), 31)

    def test_report_identifies_fixed_fallback_as_source_of_completeness(self):
        guarantee = self.report["completion_fallback_guarantee"]
        self.assertIn("nonvacuously", guarantee["condition"])
        self.assertIn("already-known", guarantee["novelty"])
        for condition in self.report["factorial"]["summary"].values():
            self.assertEqual(condition["M1_C0"]["mean_solved_fraction"], 1.0)
            self.assertEqual(condition["M1_C1"]["mean_solved_fraction"], 1.0)

    def test_artifacts_export_sources_and_specs(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = r.export_artifacts(self.report, directory)
            self.assertEqual(len(paths), len(self.report["artifacts"]) * 2)
            for path in paths:
                content = Path(path).read_text()
                if path.endswith(".json"):
                    self.assertIsInstance(json.loads(content), dict)
                else:
                    compile(content, path, "exec")

    def test_invalid_design_and_statistical_inputs(self):
        for budget in (0, 33, True, 2.5):
            with self.assertRaises(ValueError):
                r.run_pilot(units=1, budget=budget)
        for values in ([], [float("nan")], [1.1], [-1.01]):
            with self.assertRaises(ValueError):
                r.paired_hoeffding(values)
        for args in ({"candidates": 0}, {"conditions": True}, {"alpha": 1.0}):
            with self.assertRaises(ValueError):
                r.paired_hoeffding([0.0], **args)


if __name__ == "__main__":
    unittest.main()
