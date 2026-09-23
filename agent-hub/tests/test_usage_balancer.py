import math
import unittest
from dataclasses import replace

from agent_hub.usage_balancer import (
    Account, BalanceError, Concurrency, Estimate, OrchestrationReserve,
    Policy, QuotaPool, Route, Task, Worker, plan_dispatch,
)


NOW = 10000
SHA = 'a' * 64


def fixture(names=('one', 'two'), *, provider='provider'):
    workers, routes, pools, concurrency = [], [], [], []
    for name in names:
        account = Account(provider, name)
        workers.append(Worker(name, 'codex', account, 'exact-model', 'max', ('code',), NOW,
                              True, True, True, NOW, concurrency=10, last_assigned_at=NOW, fleet_plan_sha256=SHA))
        routes.append(Route(name, name, (name,), 'included'))
        pools.append(QuotaPool(name, (account,), 'included', 1, NOW, NOW + 3600, SHA))
        concurrency.append(Concurrency(provider, name, 0, 10, NOW))
    concurrency.append(Concurrency(provider, None, 0, 20, NOW))
    return tuple(workers), tuple(routes), tuple(pools), tuple(concurrency)


def run_plan(workers, routes, pools, concurrency, tasks=None, estimates=None, **kwargs):
    tasks = tasks if tasks is not None else (Task('t', NOW - 10, ('code',)),)
    if estimates is None:
        estimates = tuple(Estimate(t.id, r.id, next(w.model_id for w in workers if w.id == r.worker_id),
                                   next(w.effort for w in workers if w.id == r.worker_id),
                                   tuple((p, 0.1) for p in r.pool_ids), NOW) for t in tasks for r in routes)
    return plan_dispatch(tasks, workers, routes, pools, estimates, concurrency, now=NOW,
                         fleet_plan_sha256=SHA, policy=kwargs.pop('policy', Policy(estimate_multiplier=1)), **kwargs)


class UsageBalancerTests(unittest.TestCase):
    def test_prefers_greatest_headroom_then_rebalances_batch(self):
        w, r, p, c = fixture()
        p = (replace(p[0], remaining_fraction=0.6), replace(p[1], remaining_fraction=0.5))
        tasks = tuple(Task(f't{i}', NOW - 10 + i, ('code',)) for i in range(3))
        plan = run_plan(w, r, p, c, tasks)
        self.assertEqual([d.worker_id for d in plan.decisions], ['one', 'one', 'two'])
        self.assertAlmostEqual(dict(plan.estimated_pool_remaining)['one'], 0.4)
        self.assertEqual(p[0].remaining_fraction, 0.6)  # Inputs remain immutable.

    def test_reset_soon_gets_more_work_without_refilling_expired_pool(self):
        w, r, p, c = fixture()
        p = (replace(p[0], resets_at=NOW + 7200), replace(p[1], resets_at=NOW + 600))
        self.assertEqual(run_plan(w, r, p, c).decisions[0].worker_id, 'two')
        plan = run_plan(w, r, (replace(p[0], resets_at=NOW), p[1]), c)
        self.assertIn(('one', 'quota_reset_requires_refresh'), plan.decisions[0].rejected_routes)

    def test_demand_normalizes_different_account_allowances(self):
        w, r, p, c = fixture()
        e = (Estimate('t', 'one', 'exact-model', 'max', (('one', 0.4),), NOW),
             Estimate('t', 'two', 'exact-model', 'max', (('two', 0.1),), NOW))
        self.assertEqual(run_plan(w, r, p, c, estimates=e).decisions[0].worker_id, 'two')

    def test_aliases_share_one_pool_and_cannot_overspend_it(self):
        w, r, p, c = fixture()
        pool = replace(p[0], id='shared', accounts=(w[0].account, w[1].account), remaining_fraction=0.15)
        r = tuple(replace(v, pool_ids=('shared',)) for v in r)
        tasks = (Task('old', NOW - 20, ('code',)), Task('new', NOW - 10, ('code',)))
        plan = run_plan(w, r, (pool, pool), c, tasks)
        self.assertEqual([d.action for d in plan.decisions], ['assign', 'wait'])
        self.assertEqual(len(plan.estimated_pool_remaining), 1)
        self.assertAlmostEqual(dict(plan.estimated_pool_remaining)['shared'], 0.05)

    def test_conflicting_alias_observation_is_rejected(self):
        w, r, p, c = fixture()
        with self.assertRaises(BalanceError):
            run_plan(w, r, (*p, replace(p[0], remaining_fraction=0.3)), c)

    def test_same_provider_two_accounts_have_separate_limits(self):
        w, r, p, c = fixture(('ryan', 'blueeyes'), provider='openai')
        c = tuple(replace(v, active=10) if v.account_id in ('ryan', None) else v for v in c)
        decision = run_plan(w, r, p, c).decisions[0]
        self.assertEqual(decision.account, Account('openai', 'blueeyes'))
        self.assertIn(('ryan', 'concurrency_full'), decision.rejected_routes)

    def test_provider_concurrency_caps_all_accounts(self):
        w, r, p, c = fixture()
        c = tuple(replace(v, limit=1) if v.account_id is None else v for v in c)
        tasks = (Task('first', NOW - 20, ('code',)), Task('second', NOW - 10, ('code',)))
        self.assertEqual([d.action for d in run_plan(w, r, p, c, tasks).decisions], ['assign', 'wait'])

    def test_worker_concurrency_also_applies_across_routes(self):
        w, r, p, c = fixture(('one',))
        w = (replace(w[0], concurrency=1),)
        r = (*r, replace(r[0], id='alias'))
        tasks = (Task('first', NOW - 20, ('code',)), Task('second', NOW - 10, ('code',)))
        self.assertEqual([d.action for d in run_plan(w, r, p, c, tasks).decisions], ['assign', 'wait'])

    def credit_fixture(self):
        w, r, p, c = fixture()
        credit = replace(p[1], billing='existing_credits', resets_at=None, credits_approved=True,
                         provider_cap_enforced=True, auto_reload_disabled=True,
                         automatic_purchase_disabled=True, api_fallback_disabled=True)
        return w, (r[0], replace(r[1], billing='existing_credits')), (p[0], credit), c

    def test_included_preferred_even_with_smaller_headroom(self):
        w, r, p, c = self.credit_fixture()
        p = (replace(p[0], remaining_fraction=0.11), p[1])
        self.assertEqual(run_plan(w, r, p, c).decisions[0].billing, 'included')

    def test_existing_credits_after_included_insufficient(self):
        w, r, p, c = self.credit_fixture()
        p = (replace(p[0], remaining_fraction=0), p[1])
        self.assertEqual(run_plan(w, r, p, c).decisions[0].billing, 'existing_credits')

    def test_unresolved_included_measurement_waits_before_credit_spending(self):
        w, r, p, c = self.credit_fixture()
        for fields in ({'observed_at': NOW-901}, {'remaining_fraction': None}, {'resets_at': NOW},
                       {'evidence_sha256': None}):
            with self.subTest(fields=fields):
                d = run_plan(w, r, (replace(p[0], **fields), p[1]), c).decisions[0]
                self.assertEqual(d.reason, 'included_route_measurement_unresolved')
                self.assertIsNone(d.billing)
        credit_only_estimate = (Estimate('t', 'two', 'exact-model', 'max', (('two', 0.1),), NOW),)
        self.assertEqual(run_plan(w, r, p, c, estimates=credit_only_estimate).decisions[0].reason,
                         'included_route_measurement_unresolved')

    def test_unresolved_included_does_not_block_confirmed_included_alternative(self):
        w, r, p, c = fixture()
        p = (replace(p[0], observed_at=0), p[1])
        d = run_plan(w, r, p, c).decisions[0]
        self.assertEqual((d.action, d.worker_id, d.billing), ('assign', 'two', 'included'))

    def test_unauthenticated_or_unsuitable_included_does_not_block_credits(self):
        w, r, p, c = self.credit_fixture()
        for fields in ({'authenticated': False}, {'capabilities': ('other',)}):
            with self.subTest(fields=fields):
                d = run_plan((replace(w[0], **fields), w[1]), r,
                             (replace(p[0], observed_at=0), p[1]), c).decisions[0]
                self.assertEqual(d.billing, 'existing_credits')

    def test_busy_included_does_not_trigger_spending(self):
        w, r, p, c = self.credit_fixture()
        w = (replace(w[0], active=10), w[1])
        c = tuple(replace(v, active=10) if v.account_id in ('one', None) else v for v in c)
        self.assertEqual(run_plan(w, r, p, c).decisions[0].reason, 'included_route_busy')

    def test_existing_inflight_work_reserves_unreported_consumption(self):
        w, r, p, c = fixture(('one',))
        p = (replace(p[0], remaining_fraction=0.5, inflight_fraction=0.45),)
        plan = run_plan(w, r, p, c)
        self.assertEqual(plan.decisions[0].action, 'wait')
        self.assertAlmostEqual(dict(plan.estimated_pool_remaining)['one'], 0.05)

    def test_inconsistent_concurrency_receipts_rejected(self):
        w, r, p, c = fixture(('one',))
        with self.assertRaises(BalanceError):
            run_plan((replace(w[0], active=1),), r, p, c)
        with self.assertRaises(BalanceError):
            run_plan(w, r, p, (replace(c[0], active=1), c[1]))

    def test_every_existing_credit_control_required(self):
        w, r, p, c = self.credit_fixture()
        for flag in ('credits_approved', 'provider_cap_enforced', 'auto_reload_disabled',
                     'automatic_purchase_disabled', 'api_fallback_disabled'):
            with self.subTest(flag=flag):
                plan = run_plan(w, r, (replace(p[0], remaining_fraction=0), replace(p[1], **{flag: False})), c)
                self.assertEqual(plan.decisions[0].action, 'wait')
                self.assertIn(('two', 'existing_credit_controls_unverified'), plan.decisions[0].rejected_routes)

    def test_unknown_or_unlimited_never_treated_free(self):
        w, r, p, c = fixture(('one',))
        for billing in ('unknown', 'unlimited'):
            decision = run_plan(w, r, (replace(p[0], billing=billing),), c).decisions[0]
            self.assertEqual(decision.action, 'wait')
            self.assertIn(('one', 'unbounded_or_unknown_billing_pool'), decision.rejected_routes)

    def test_missing_stale_future_usage_and_unknown_resets_block(self):
        w, r, p, c = fixture(('one',))
        cases = ({'remaining_fraction': None}, {'evidence_sha256': None}, {'observed_at': NOW-901},
                 {'observed_at': NOW+1}, {'resets_at': None}, {'status': 'unavailable'})
        for fields in cases:
            with self.subTest(fields=fields):
                self.assertEqual(run_plan(w, r, (replace(p[0], **fields),), c).decisions[0].action, 'wait')

    def test_unknown_auth_maximum_model_or_unsuitable_worker_not_chosen(self):
        w, r, p, c = fixture(('one',))
        for fields in ({'authenticated': False}, {'maximum_model_verified': False}, {'available': False},
                       {'model_observed_at': NOW-901}, {'observed_at': NOW-91}, {'capabilities': ('other',)}):
            with self.subTest(fields=fields):
                self.assertEqual(run_plan((replace(w[0], **fields),), r, p, c).decisions[0].action, 'wait')

    def test_model_effort_is_preserved_never_downgraded(self):
        w, r, p, c = fixture(('one',))
        w = (replace(w[0], model_id='strongest-model[1m]', effort='ultra'),)
        d = run_plan(w, r, p, c).decisions[0]
        self.assertEqual((d.model_id, d.effort), ('strongest-model[1m]', 'ultra'))
        est = (Estimate('t', 'one', 'cheap-model', 'low', (('one', 0.001),), NOW),)
        self.assertEqual(run_plan(w, r, p, c, estimates=est).decisions[0].action, 'wait')

    def test_independent_or_missing_fleet_model_plan_is_not_eligible(self):
        w, r, p, c = fixture(('one',))
        for digest in (None, 'b' * 64):
            d = run_plan((replace(w[0], fleet_plan_sha256=digest),), r, p, c).decisions[0]
            self.assertEqual(d.action, 'wait')
            self.assertIn(('one', 'fleet_model_plan_mismatch'), d.rejected_routes)

    def test_missing_estimate_or_pool_window_is_not_zero_cost(self):
        w, r, p, c = fixture(('one',))
        for e in ((), (Estimate('t', 'one', 'exact-model', 'max', (), NOW),),
                  (Estimate('t', 'one', 'exact-model', 'max', (('one', 0.1),), 0),)):
            policy = Policy(max_estimate_age=60)
            self.assertEqual(run_plan(w, r, p, c, estimates=e, policy=policy).decisions[0].action, 'wait')

    def test_multiple_windows_use_limiting_window(self):
        w, r, p, c = fixture(('one',))
        weekly = replace(p[0], id='weekly', remaining_fraction=0.01)
        r = (replace(r[0], pool_ids=('one', 'weekly')),)
        self.assertEqual(run_plan(w, r, (*p, weekly), c).decisions[0].action, 'wait')

    def test_uncertain_charge_never_retries_any_provider(self):
        w, r, p, c = fixture()
        t = (Task('t', NOW-100, ('code',), prior_attempt='uncertain'),)
        d = run_plan(w, r, p, c, t).decisions[0]
        self.assertEqual(d.action, 'blocked')
        self.assertEqual(d.reason, 'uncertain_charge_requires_reconciliation')

    def test_fifo_old_task_gets_last_slot_and_unfit_old_does_not_block(self):
        w, r, p, c = fixture(('one',))
        w = (replace(w[0], concurrency=1),)
        t = (Task('new', NOW-10, ('code',)), Task('old', NOW-100, ('code',)), Task('unfit', NOW-200, ('other',)))
        d = run_plan(w, r, p, c, t).decisions
        self.assertEqual([(v.task_id, v.action) for v in d], [('unfit', 'wait'), ('old', 'assign'), ('new', 'wait')])

    def test_idle_worker_fairness_turn_stays_included_and_eligible(self):
        w, r, p, c = fixture()
        w = (w[0], replace(w[1], last_assigned_at=NOW-4000))
        p = (p[0], replace(p[1], remaining_fraction=0.11))
        d = run_plan(w, r, p, c).decisions[0]
        self.assertEqual(d.worker_id, 'two')
        self.assertEqual(d.reason, 'oldest_eligible_worker_fairness_turn')

    def test_chatgpt_origin_does_not_imply_codex_quota_sharing(self):
        w, r, p, c = fixture(('one',), provider='openai')
        t = (Task('t', NOW-10, ('code',), origin_account='blueeyes-chatgpt'),)
        plan = run_plan(w, r, p, c, t)
        self.assertEqual(plan.decisions[0].origin_account, 'blueeyes-chatgpt')
        self.assertEqual(plan.orchestration_reserved, ())
        self.assertIn('chatgpt_conversation_usage_unavailable:blueeyes-chatgpt', plan.notices)
        self.assertEqual(plan.decisions[0].account.account_id, 'one')

    def test_explicit_shared_pool_binding_reserves_orchestration_headroom(self):
        w, r, p, c = fixture(('one',))
        reserve = (OrchestrationReserve('chat-account', 'one', 0.95, NOW, SHA),)
        plan = run_plan(w, r, p, c, orchestration_reserves=reserve)
        self.assertEqual(plan.decisions[0].action, 'wait')
        self.assertEqual(plan.orchestration_reserved, (('one', 0.95),))
        stale = (replace(reserve[0], observed_at=NOW-901),)
        d = run_plan(w, r, p, c, orchestration_reserves=stale).decisions[0]
        self.assertIn(('one', 'orchestration_binding_stale'), d.rejected_routes)

    def test_missing_or_stale_concurrency_does_not_mean_unlimited(self):
        w, r, p, c = fixture(('one',))
        for limits in ((), (c[0],), tuple(replace(v, observed_at=NOW-91) for v in c)):
            self.assertEqual(run_plan(w, r, p, limits).decisions[0].action, 'wait')

    def test_validation_rejects_nan_negative_bool_and_unknown_members(self):
        w, r, p, c = fixture(('one',))
        for value in (math.nan, math.inf, -0.1, True):
            with self.assertRaises(BalanceError):
                replace(p[0], remaining_fraction=value)
        with self.assertRaises(BalanceError):
            run_plan(w, r, (replace(p[0], accounts=(Account('other', 'account'),)),), c)
        with self.assertRaises(BalanceError):
            replace(r[0], pool_ids=('one', 'one'))

    def test_safety_multiplier_and_batch_limit(self):
        w, r, p, c = fixture(('one',))
        p = (replace(p[0], remaining_fraction=0.11),)
        self.assertEqual(run_plan(w, r, p, c, policy=Policy()).decisions[0].action, 'wait')
        self.assertEqual(run_plan(w, r, p, c, policy=Policy(max_assignments=0)).decisions[0].reason, 'batch_limit')


if __name__ == '__main__':
    unittest.main()
