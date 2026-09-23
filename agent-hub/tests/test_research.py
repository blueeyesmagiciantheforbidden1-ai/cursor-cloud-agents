import concurrent.futures
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_hub.research import ResearchArchive, ResearchError, content_hash, hoeffding_lower_bound


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "research.sqlite3"
        self.archive = ResearchArchive(self.path)
        self.policy = {
            "name": "navigation-v1", "sample_size": 64, "max_candidates": 8,
            "alpha": 0.05, "meaningful_delta": 0.1, "budget_per_arm": 64,
            "evaluator_digest": content_hash({"evaluator": "fixed-v1"}),
            "protected_contract_digest": content_hash({"contracts": ["no-secret-leak", "tests-pass"]}),
            "guard_names": ["security", "correctness"],
        }
        self.epoch = self.archive.create_epoch(self.policy)
        self.baseline = self.candidate("baseline")

    def candidate(self, name, **kwargs):
        return self.archive.add_candidate("task_agent", {"name": name, "revision": "frozen"},
                                          proposer="builder", **kwargs)

    def trial(self, name="candidate", *, candidate=None, prefix=None, **kwargs):
        candidate = candidate or self.candidate(name, parents=[self.baseline["id"]])
        plan = self.archive.preregister_trial(
            self.epoch["id"], candidate["id"], self.baseline["id"],
            [f"{prefix or name}-holdout-{i}" for i in range(64)],
            independent_evaluator="audit-worker", **kwargs)
        return candidate, plan

    def pair(self, plan, index, **overrides):
        data = dict(candidate_score=0.9, baseline_score=0.1, candidate_cost=1, baseline_cost=1,
                    candidate_latency_ms=10.0, candidate_robustness=0.9,
                    contracts_pass=True, guard_deltas={"security": 0.0, "correctness": 0.0},
                    evaluator_digest=self.policy["evaluator_digest"],
                    protected_contract_digest=self.policy["protected_contract_digest"],
                    candidate_content_hash=plan["candidate_content_hash"],
                    baseline_content_hash=plan["baseline_content_hash"])
        data.update(overrides)
        return self.archive.record_pair(plan["id"], plan["holdout_ids"][index], **data)

    def complete(self, plan, **overrides):
        for index in range(64):
            self.pair(plan, index, **overrides)
        return self.archive.finalize_trial(plan["id"])

    def test_candidate_is_content_addressed_and_deeply_immutable(self):
        artifact = {"source": "return 1"}
        candidate = self.archive.add_candidate("task_agent", artifact)
        artifact["source"] = "return 2"
        candidate["artifact"]["source"] = "return 3"
        reread = self.archive.get("candidate", candidate["id"])
        self.assertEqual(reread["artifact"]["source"], "return 1")
        self.assertEqual(reread["content_hash"], content_hash({"source": "return 1"}))
        with self.assertRaisesRegex(ResearchError, "content hash mismatch"):
            self.archive.add_candidate("task_agent", artifact, expected_content_hash=reread["content_hash"])
        with self.assertRaises(ResearchError):
            self.candidate("unknown-parent", parents=["0" * 64])
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE research_records SET data='{}' WHERE id=?", (candidate["id"],))
        finally:
            connection.close()

    def test_policy_digest_and_protected_obligations_cannot_drift(self):
        self.assertEqual(self.archive.create_epoch(self.policy)["id"], self.epoch["id"])
        policy = {**self.policy, "evaluator_digest": "b" * 64}
        with self.assertRaisesRegex(ResearchError, "policy drift"):
            self.archive.create_epoch(policy)
        with self.assertRaisesRegex(ResearchError, "cannot change"):
            self.archive.create_epoch({**self.policy, "name": "new", "protected_contract_digest": "b" * 64})
        with self.assertRaisesRegex(ResearchError, "cannot change"):
            self.archive.create_epoch({**self.policy, "name": "new", "guard_names": ["security"]})
        newer = self.archive.create_epoch({**policy, "name": "new-evaluator-epoch"})
        self.assertNotEqual(newer["id"], self.epoch["id"])

    def test_requires_fresh_fixed_holdouts_and_independent_evaluator(self):
        candidate, plan = self.trial()
        for evaluator in ("builder",):
            with self.assertRaisesRegex(ResearchError, "independent"):
                self.archive.preregister_trial(self.epoch["id"], candidate["id"], self.baseline["id"],
                                               plan["holdout_ids"], independent_evaluator=evaluator)
        second = self.candidate("second")
        with self.assertRaisesRegex(ResearchError, "already used"):
            self.archive.preregister_trial(self.epoch["id"], second["id"], self.baseline["id"],
                                           plan["holdout_ids"], independent_evaluator="audit-worker")
        # A failed reservation rolls back the candidate slot and all new holdouts.
        _, second_plan = self.trial("second", candidate=second)
        self.assertNotEqual(plan["id"], second_plan["id"])

    def test_concurrent_holdout_reservation_has_one_winner(self):
        variants = [self.candidate(f"parallel-{i}") for i in range(2)]
        def attempt(candidate):
            try:
                self.trial(candidate=candidate, prefix="same-audit")
                return True
            except ResearchError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, variants)), 1)

    def test_actual_evaluator_and_source_hash_must_match_preregistration(self):
        _, plan = self.trial()
        for bad in ({"evaluator_digest": "0" * 64}, {"protected_contract_digest": "0" * 64},
                    {"candidate_content_hash": "0" * 64}, {"baseline_content_hash": "0" * 64}):
            with self.subTest(bad=bad), self.assertRaises(ResearchError):
                self.pair(plan, 0, **bad)
        self.assertEqual(self.pair(plan, 0)["recorded_pairs"], 1)

    def test_bounded_score_schema_budget_and_duplicate_observations(self):
        _, plan = self.trial()
        for bad in ({"candidate_score": 1.1}, {"baseline_score": -0.01}, {"candidate_score": float("nan")},
                    {"candidate_cost": 65}, {"candidate_cost": True}, {"guard_deltas": {"security": 0}},
                    {"contracts_pass": "true"}):
            with self.subTest(bad=bad), self.assertRaises(ResearchError):
                self.pair(plan, 0, **bad)
        self.pair(plan, 0)
        with self.assertRaises(ResearchError):
            self.pair(plan, 0)
        with self.assertRaisesRegex(ResearchError, "budget exceeded"):
            self.pair(plan, 1, candidate_cost=64)

    def test_no_peeking_promotion_fixed_sample_multiplicity_and_success(self):
        _, plan = self.trial()
        for index in range(63):
            self.pair(plan, index)
        with self.assertRaisesRegex(ResearchError, "Fixed sample incomplete"):
            self.archive.finalize_trial(plan["id"])
        self.pair(plan, 63)
        result = self.archive.finalize_trial(plan["id"])
        expected = 0.8 - math.sqrt(2 * math.log(8 / 0.05) / 64)
        self.assertAlmostEqual(result["lower_bound"], expected)
        self.assertEqual(result["alpha_per_candidate"], 0.05 / 8)
        self.assertTrue(result["eligible"])
        self.assertEqual(self.archive.finalize_trial(plan["id"]), result)

    def test_small_positive_mean_is_not_false_promoted(self):
        _, plan = self.trial()
        result = self.complete(plan, candidate_score=0.6, baseline_score=0.5)
        self.assertGreater(result["mean_delta"], 0)
        self.assertFalse(result["eligible"])
        self.assertIn("lower_bound_not_above_meaningful_delta", result["reasons"])

    def test_contract_failure_or_single_regression_blocks_promotion(self):
        _, plan = self.trial("contract-failure")
        self.pair(plan, 0, contracts_pass=False)
        for i in range(1, 64):
            self.pair(plan, i)
        result = self.archive.finalize_trial(plan["id"])
        self.assertFalse(result["eligible"])
        self.assertFalse(result["valid"])
        _, other = self.trial("regression")
        self.pair(other, 0, guard_deltas={"security": -0.001, "correctness": 0})
        for i in range(1, 64):
            self.pair(other, i)
        result = self.archive.finalize_trial(other["id"])
        self.assertFalse(result["eligible"])
        self.assertIn("observed_regression", result["reasons"])

    def test_underbudget_or_unequal_actual_costs_do_not_qualify(self):
        _, plan = self.trial()
        result = self.complete(plan, candidate_cost=0)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["equal_total_budget"])
        self.assertEqual(result["actual_cost"], {"candidate": 0, "baseline": 64})

    def test_generation_overhead_counts_in_equal_budget(self):
        _, plan = self.trial(candidate_overhead=1)
        self.pair(plan, 0, candidate_cost=0)
        for i in range(1, 64):
            self.pair(plan, i)
        result = self.archive.finalize_trial(plan["id"])
        self.assertTrue(result["equal_total_budget"])
        self.assertEqual(result["actual_cost"]["candidate"], 64)

    def test_epoch_candidate_limit_counts_failed_and_incomplete_trials(self):
        policy = {**self.policy, "name": "one-attempt", "max_candidates": 1}
        epoch = self.archive.create_epoch(policy)
        first, second = self.candidate("one"), self.candidate("two")
        self.archive.preregister_trial(epoch["id"], first["id"], self.baseline["id"],
                                       [f"one-{i}" for i in range(64)], independent_evaluator="auditor")
        with self.assertRaisesRegex(ResearchError, "candidate limit"):
            self.archive.preregister_trial(epoch["id"], second["id"], self.baseline["id"],
                                           [f"two-{i}" for i in range(64)], independent_evaluator="auditor")

    def test_pareto_preserves_tradeoffs_and_quality_diversity_niches(self):
        slow = self.candidate("slow", niche="debugging")
        fast = self.candidate("fast", niche="navigation")
        dominated = self.candidate("dominated", niche="debugging")
        for name, candidate, correctness, latency in (("slow", slow, .95, 20),
                                                     ("fast", fast, .8, 5),
                                                     ("dominated", dominated, .7, 30)):
            _, plan = self.trial(name, candidate=candidate)
            self.complete(plan, candidate_score=correctness, candidate_latency_ms=latency)
        frontier = self.archive.pareto_frontier(self.epoch["id"])
        self.assertEqual({row["candidate_id"] for row in frontier}, {slow["id"], fast["id"]})
        niches = self.archive.quality_diversity(self.epoch["id"])
        self.assertEqual({row["niche"] for row in niches}, {"debugging", "navigation"})
        self.assertNotIn(dominated["id"], {row["candidate_id"] for row in niches})

    def test_metaproductivity_is_descendant_performance_at_fixed_total_budget(self):
        procedure = self.archive.add_candidate("improvement_procedure", {"search": "prefer-callers"})
        search = self.archive.register_search(self.epoch["id"], procedure["id"], self.baseline["id"], 2)
        with self.assertRaisesRegex(ResearchError, "not a descendant"):
            self.trial("unrelated", search_id=search["id"])
        for i in range(2):
            candidate = self.candidate(f"descendant-{i}", parents=[self.baseline["id"], procedure["id"]])
            _, plan = self.trial(f"descendant-{i}", candidate=candidate, search_id=search["id"])
            self.complete(plan, candidate_score=0.9 + 0.05 * i)
            result = self.archive.metaproductivity(search["id"])
            self.assertEqual(result["complete"], i == 1)
            if i == 0:
                self.assertNotIn("best_descendant_utility", result)
        self.assertEqual(result["actual_cost"], 256)
        self.assertEqual(result["fixed_budget"], 256)
        self.assertAlmostEqual(result["best_descendant_utility"], .95)
        self.assertTrue(result["valid"])

    def test_intervention_replay_refuses_unknown_actions_and_reports_coverage(self):
        _, plan = self.trial()
        self.complete(plan)
        action = self.archive.record_intervention(plan["id"], change="Include callers in context",
                hypothesis="Missing call sites caused failures", conditions="Small Python repositories",
                helped="64 matched checks", failed="No broader claim")
        known = self.archive.replay([action["id"]])
        self.assertEqual(known["coverage"], 1)
        with self.assertRaisesRegex(ResearchError, "unrecorded action"):
            self.archive.replay([action["id"], "0" * 64])
        partial = self.archive.replay([action["id"], "0" * 64], strict=False)
        self.assertEqual(partial["coverage"], .5)
        self.assertEqual(len(partial["observations"]), 1)
        self.assertFalse(partial["complete"])

    def test_matched_interaction_and_no_reuse_for_confirmatory_holdout(self):
        candidates = {"baseline": self.baseline["id"]}
        for cell in ("a", "b", "ab"):
            candidates[cell] = self.candidate(cell, parents=[self.baseline["id"]])["id"]
        kwargs = dict(epoch_id=self.epoch["id"], candidate_ids=candidates, block_ids=["explore-1", "explore-2"],
                      scores={"baseline": [.2, .2], "a": [.3, .3], "b": [.4, .4], "ab": [.8, .8]},
                      costs={cell: 2 for cell in candidates}, evaluator_digest=self.policy["evaluator_digest"])
        result = self.archive.record_interaction(**kwargs)
        self.assertAlmostEqual(result["mean_interaction"], .3)
        self.assertFalse(result["confirmatory"])
        with self.assertRaisesRegex(ResearchError, "already used"):
            self.archive.preregister_trial(self.epoch["id"], candidates["a"], self.baseline["id"],
                ["explore-1"] + [f"fresh-{i}" for i in range(63)], independent_evaluator="auditor")
        kwargs["block_ids"] = ["new-1", "new-2"]
        kwargs["costs"]["a"] = 3
        with self.assertRaisesRegex(ResearchError, "equal total budgets"):
            self.archive.record_interaction(**kwargs)

    def test_abstraction_requires_source_bound_passing_test_artifact(self):
        candidate, plan = self.trial()
        self.complete(plan)
        evidence = {"passed": True, "source_hash": candidate["content_hash"],
                    "contract_digest": self.policy["protected_contract_digest"],
                    "checks": [{"name": "reject-stale-context", "passed": True}]}
        abstraction = self.archive.record_abstraction(candidate["id"], plan["id"],
                interface="compile_context(manifest) -> bounded verified context", test_artifact=evidence)
        self.assertEqual(abstraction["test_artifact_hash"], content_hash(evidence))
        evidence["source_hash"] = "0" * 64
        with self.assertRaisesRegex(ResearchError, "match source"):
            self.archive.record_abstraction(candidate["id"], plan["id"], interface="other", test_artifact=evidence)

    def test_restart_preserves_trials_and_hash_verified_results(self):
        _, plan = self.trial()
        result = self.complete(plan)
        reopened = ResearchArchive(self.path)
        self.assertEqual(reopened.finalize_trial(plan["id"]), result)
        self.assertEqual(reopened.get("trial", plan["id"])["candidate_id"], plan["candidate_id"])

    def test_hoeffding_rejects_out_of_range_and_handles_worst_case(self):
        self.assertEqual(hoeffding_lower_bound([-1] * 10, .05), -1)
        with self.assertRaises(ResearchError):
            hoeffding_lower_bound([1.0001], .05)


if __name__ == "__main__":
    unittest.main()
