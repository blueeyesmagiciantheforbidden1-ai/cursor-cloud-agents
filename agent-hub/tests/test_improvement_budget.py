from dataclasses import replace
import unittest

from agent_hub.usage_balancer import BalanceError, Estimate, Policy, Task
from tests.test_usage_balancer import fixture, run_plan, NOW


class ImprovementBudgetTests(unittest.TestCase):
    def plan(self, *, committed=0, remaining=1, tasks=None, demand=0.02, policy=None, pool_fields=None):
        workers, routes, pools, concurrency = fixture(('one',))
        tasks = tasks or (Task('improve', NOW - 100, ('code',), purpose='improvement'),)
        pools = (replace(pools[0], improvement_committed_fraction=committed,
                         remaining_fraction=remaining, **(pool_fields or {})),)
        estimates = tuple(Estimate(t.id, routes[0].id, workers[0].model_id, workers[0].effort,
                                  (('one', demand),), NOW) for t in tasks)
        return run_plan(workers, routes, pools, concurrency, tasks, estimates,
                        policy=policy or Policy(estimate_multiplier=1))

    def test_improvement_is_capped_even_with_spare_subscription(self):
        decision = self.plan(committed=0.24, demand=0.02).decisions[0]
        self.assertEqual(decision.action, 'wait')
        self.assertIn(('one', 'improvement_budget_exhausted'), decision.rejected_routes)
        self.assertEqual(self.plan(committed=0.23, demand=0.02).decisions[0].action, 'assign')

    def test_project_work_overtakes_older_improvement(self):
        tasks = (Task('improve', NOW - 100, ('code',), purpose='improvement'),
                 Task('project', NOW - 1, ('code',)))
        decisions = {d.task_id: d for d in self.plan(tasks=tasks).decisions}
        self.assertEqual(decisions['improve'].reason, 'foreground_work_has_priority')
        self.assertEqual(decisions['project'].action, 'assign')

    def test_active_foreground_also_blocks_background(self):
        result = self.plan(policy=Policy(foreground_active=True)).decisions[0]
        self.assertEqual(result.reason, 'foreground_work_has_priority')

    def test_unknown_ledger_is_not_free_improvement_credit(self):
        result = self.plan(committed=None).decisions[0]
        self.assertEqual(result.action, 'wait')
        self.assertIn(('one', 'improvement_ledger_unknown'), result.rejected_routes)

    def test_unknown_or_stale_allowance_cannot_launch(self):
        self.assertEqual(self.plan(remaining=None).decisions[0].action, 'wait')
        self.assertEqual(self.plan(pool_fields={'observed_at': NOW - 901}).decisions[0].action, 'wait')

    def test_batch_shares_one_improvement_budget(self):
        tasks = tuple(Task(str(i), NOW - 100 + i, ('code',), purpose='improvement') for i in range(3))
        self.assertEqual([d.action for d in self.plan(tasks=tasks, demand=0.10).decisions], ['assign', 'assign', 'wait'])

    def test_preserves_quarter_allowance_headroom_for_urgent_work(self):
        decision = self.plan(remaining=0.26).decisions[0]
        self.assertEqual(decision.action, 'wait')
        self.assertIn(('one', 'foreground_allowance_reserve'), decision.rejected_routes)

    def test_improvement_never_routes_to_existing_paid_credit(self):
        workers, routes, pools, concurrency = fixture(('one',))
        routes = (replace(routes[0], billing='existing_credits'),)
        pools = (replace(pools[0], billing='existing_credits', resets_at=None,
                         improvement_committed_fraction=0, credits_approved=True,
                         provider_cap_enforced=True, auto_reload_disabled=True,
                         automatic_purchase_disabled=True, api_fallback_disabled=True),)
        decision = run_plan(workers, routes, pools, concurrency,
                            (Task('t', NOW-1, ('code',), purpose='improvement'),)).decisions[0]
        self.assertIn(('one', 'improvement_never_uses_purchased_credits'), decision.rejected_routes)
        self.assertEqual(decision.action, 'wait')

    def test_policy_can_tighten_but_not_enlarge_budget(self):
        self.assertEqual(self.plan(policy=Policy(improvement_fraction_cap=0)).decisions[0].action, 'wait')
        with self.assertRaises(BalanceError):
            Policy(improvement_fraction_cap=0.5)

    def test_pending_uncertain_improvement_is_never_replayed(self):
        task = Task('i', NOW - 1, ('code',), prior_attempt='uncertain', purpose='improvement')
        self.assertEqual(self.plan(tasks=(task,)).decisions[0].reason, 'uncertain_charge_requires_reconciliation')


if __name__ == '__main__':
    unittest.main()
