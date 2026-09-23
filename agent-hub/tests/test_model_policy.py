"""Synthetic catalog evidence only: no provider access, inference or secrets."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from agent_hub.model_policy import (AGENTS, PolicyError, SelectionPolicy, cli_selection_args,
                                    select_stack, validate_plan, verify_observed_selection)


NOW = 2000000000
HASH = 'a' * 64
CONTROLS = {'codex': 'codex-config', 'claude': 'claude-effort', 'cursor': 'model-variant',
            'copilot': 'copilot-effort', 'grok': 'grok-effort'}


def candidate(agent, canonical, rank=1, *, levels=('low', 'high')):
    cli_id = canonical.replace(':', '-')
    return {'model_ref': canonical, 'strength_rank': rank, 'accessible': True,
            'billing': 'subscription_included', 'allowance': 'available', 'effort_control': CONTROLS[agent],
            'effective_capabilities_verified': True, 'capability_evidence_sha256': HASH,
            'efforts': [{'level': level, 'cli_model_id': cli_id + ('-' + level if agent == 'cursor' else ''),
                         'pin_verified': True, 'model_ref': canonical} for level in levels]}


def bundle(rows=None):
    rows = rows or {agent: [candidate(agent, 'synthetic:' + agent)] for agent in AGENTS}
    identities = {row['model_ref']: {'family': row['model_ref'], 'identity_evidence_sha256': HASH}
                  for candidates in rows.values() for row in candidates}
    return {'schema_version': 1,
            'registry': {'observed_at': NOW - 10, 'expires_at': NOW + 1000, 'revision': 'identity-v1',
                         'evidence_sha256': HASH, 'models': identities, 'aliases': {}},
            'catalogs': {agent: {'observed_at': NOW - 10, 'expires_at': NOW + 1000, 'revision': 'catalog-v1',
                                'evidence_sha256': HASH, 'complete': True, 'source': 'account_catalog',
                                'auth_route': 'subscription', 'overage_disabled': True,
                                'account_ref': HASH, 'cli_sha256': HASH, 'cli_version': 'synthetic-1.0',
                                'ranking_evidence_sha256': HASH, 'candidates': candidates}
                         for agent, candidates in rows.items()}}


class ModelPolicyTests(unittest.TestCase):
    def setUp(self):
        deny = patch('socket.socket.connect', side_effect=AssertionError('Policy tests must stay offline'))
        deny.start()
        self.addCleanup(deny.stop)

    def test_five_distinct_models_at_each_providers_actual_highest_effort(self):
        data = bundle()
        data['catalogs']['codex']['candidates'][0] = candidate('codex', 'synthetic:codex', levels=('low', 'xhigh', 'ultra'))
        data['catalogs']['claude']['candidates'][0] = candidate('claude', 'synthetic:claude', levels=('high', 'max'))
        plan = select_stack(data, now=NOW)
        self.assertEqual(plan['distinct_canonical_models'], 5)
        self.assertEqual(plan['distinct_families'], 5)
        self.assertEqual(plan['selections']['codex']['effort'], 'ultra')
        self.assertEqual(plan['selections']['claude']['effort'], 'max')
        self.assertEqual(plan['selections']['grok']['effort'], 'high')
        self.assertFalse(plan['live_catalog_fetchers_implemented'])
        self.assertFalse(plan['subscription_only'])
        self.assertEqual(plan['billing_policy'], 'included_then_existing_credits')
        self.assertFalse(plan['api_billing_allowed'])
        self.assertTrue(validate_plan(plan, data, now=NOW + 1))

    def test_global_assignment_avoids_greedy_collision(self):
        data = bundle({'codex': [candidate('codex', 'synthetic:shared'), candidate('codex', 'synthetic:other', 2)],
                       'copilot': [candidate('copilot', 'synthetic:shared')]})
        plan = select_stack(data, agents=('codex', 'copilot'), now=NOW)
        self.assertEqual(plan['selections']['codex']['canonical_id'], 'synthetic:other')
        self.assertEqual(plan['selections']['copilot']['canonical_id'], 'synthetic:shared')
        self.assertEqual(plan['strength_loss_profile'], [1, 0])

    def test_aliases_across_products_cannot_bypass_unique_identity(self):
        data = bundle({'codex': [candidate('codex', 'synthetic:shared')],
                       'copilot': [candidate('copilot', 'synthetic:shared')]})
        row = data['catalogs']['copilot']['candidates'][0]
        row['model_ref'] = 'copilot:alias'
        for option in row['efforts']:
            option['model_ref'] = 'copilot:alias'
        data['registry']['aliases'] = {'copilot:alias': 'other:alias', 'other:alias': 'synthetic:shared'}
        with self.assertRaises(PolicyError) as error:
            select_stack(data, agents=('codex', 'copilot'), now=NOW)
        self.assertEqual(error.exception.code, 'no_distinct_stack')

    def test_same_agent_aliases_are_deduplicated_and_conflicts_block(self):
        data = bundle({'codex': [candidate('codex', 'synthetic:a')]})
        alias = deepcopy(data['catalogs']['codex']['candidates'][0])
        alias['model_ref'] = 'surface:alias'
        data['registry']['aliases']['surface:alias'] = 'synthetic:a'
        data['catalogs']['codex']['candidates'].append(alias)
        plan = select_stack(data, agents=('codex',), now=NOW)
        self.assertEqual(plan['search_states'], 1)
        alias['strength_rank'] = 2
        with self.assertRaises(PolicyError) as error:
            select_stack(data, agents=('codex',), now=NOW)
        self.assertEqual(error.exception.code, 'conflicting_alias_capabilities')

    def test_family_diversity_breaks_equal_strength_ties(self):
        data = bundle({'codex': [candidate('codex', 'synthetic:a')],
                       'cursor': [candidate('cursor', 'synthetic:b'), candidate('cursor', 'synthetic:z')]})
        data['registry']['models']['synthetic:b']['family'] = 'synthetic:a'
        plan = select_stack(data, agents=('codex', 'cursor'), now=NOW)
        self.assertEqual(plan['selections']['cursor']['canonical_id'], 'synthetic:z')
        data['catalogs']['cursor']['candidates'][1]['strength_rank'] = 2
        stronger = select_stack(data, agents=('codex', 'cursor'), now=NOW)
        self.assertEqual(stronger['selections']['cursor']['canonical_id'], 'synthetic:b')

    def test_input_order_does_not_change_optimal_assignment(self):
        data = bundle({'codex': [candidate('codex', 'synthetic:a'), candidate('codex', 'synthetic:b', 2)],
                       'copilot': [candidate('copilot', 'synthetic:a'), candidate('copilot', 'synthetic:c', 2)]})
        first = select_stack(data, agents=('codex', 'copilot'), now=NOW)
        for catalog in data['catalogs'].values():
            catalog['candidates'].reverse()
        second = select_stack(data, agents=('copilot', 'codex'), now=NOW)
        self.assertEqual(first['selections'], second['selections'])

    def test_new_verified_stronger_model_replaces_old_without_hardcoded_name(self):
        data = bundle({'codex': [candidate('codex', 'synthetic:old', 2)]})
        old = select_stack(data, agents=('codex',), now=NOW)
        data['registry']['models']['synthetic:future'] = {'family': 'synthetic:next', 'identity_evidence_sha256': HASH}
        data['catalogs']['codex']['candidates'].append(candidate('codex', 'synthetic:future', 1, levels=('high', 'future_effort')))
        data['catalogs']['codex']['revision'] = 'catalog-v2'
        new = select_stack(data, agents=('codex',), now=NOW)
        self.assertEqual(new['selections']['codex']['canonical_id'], 'synthetic:future')
        self.assertEqual(new['selections']['codex']['effort'], 'future_effort')
        with self.assertRaises(PolicyError):
            validate_plan(old, data, now=NOW)

    def test_unknown_eligible_new_model_rank_does_not_silently_use_old_model(self):
        data = bundle({'codex': [candidate('codex', 'synthetic:old')]})
        row = candidate('codex', 'synthetic:future')
        row['strength_rank'] = None
        data['registry']['models']['synthetic:future'] = {'family': 'synthetic:next', 'identity_evidence_sha256': HASH}
        data['catalogs']['codex']['candidates'].append(row)
        with self.assertRaises(PolicyError) as error:
            select_stack(data, agents=('codex',), now=NOW)
        self.assertEqual(error.exception.code, 'unknown_strength_rank')

    def test_known_paid_unavailable_or_exhausted_candidates_are_excluded(self):
        for field, value in (('billing', 'api_metered'), ('billing', 'requires_overage'),
                             ('accessible', False), ('allowance', 'exhausted')):
            data = bundle({'claude': [candidate('claude', 'synthetic:included', 2)]})
            paid = candidate('claude', 'synthetic:premium', 1)
            paid[field] = value
            data['catalogs']['claude']['candidates'].append(paid)
            plan = select_stack(data, agents=('claude',), now=NOW)
            self.assertEqual(plan['selections']['claude']['canonical_id'], 'synthetic:included')
            self.assertEqual(len(plan['exclusions']['claude']), 1)

    def test_unknown_cost_access_effort_or_allowance_blocks_all_selection(self):
        for field, value in (('billing', 'unknown'), ('accessible', None), ('allowance', 'unknown'),
                             ('efforts', []), ('effective_capabilities_verified', False), ('strength_rank', True)):
            data = bundle({'claude': [candidate('claude', 'synthetic:a')]})
            data['catalogs']['claude']['candidates'][0][field] = value
            with self.subTest(field=field), self.assertRaises(PolicyError):
                select_stack(data, agents=('claude',), now=NOW)

    def test_missing_account_catalog_or_cost_enforcement_fails_closed(self):
        for field, value in (('complete', False), ('source', 'public_docs'), ('auth_route', 'api'),
                             ('overage_disabled', False), ('cli_sha256', None), ('ranking_evidence_sha256', None)):
            data = bundle()
            data['catalogs']['grok'][field] = value
            with self.subTest(field=field), self.assertRaises(PolicyError):
                select_stack(data, now=NOW)
        data = bundle()
        del data['catalogs']['cursor']
        with self.assertRaises(PolicyError):
            select_stack(data, now=NOW)

    def test_stale_future_expired_identity_or_account_evidence_blocks(self):
        for location in ('registry', 'catalog'):
            for values in ({'expires_at': NOW}, {'observed_at': NOW + 1}, {'observed_at': NOW - 3600}):
                data = bundle()
                record = data['registry'] if location == 'registry' else data['catalogs']['codex']
                record.update(values)
                with self.subTest(location=location, values=values), self.assertRaises(PolicyError):
                    select_stack(data, now=NOW)

    def test_unresolved_and_cyclic_aliases_are_rejected(self):
        for aliases in ({'alias:a': 'alias:b', 'alias:b': 'alias:a'}, {'alias:a': 'missing:model'}):
            data = bundle()
            data['registry']['aliases'] = aliases
            with self.assertRaises(PolicyError):
                select_stack(data, now=NOW)

    def test_cost_policy_can_exclude_model_or_family(self):
        data = bundle({'grok': [candidate('grok', 'synthetic:top'), candidate('grok', 'synthetic:other', 2)]})
        for policy in (SelectionPolicy(excluded_canonical_ids=('synthetic:top',)),
                       SelectionPolicy(excluded_families=('synthetic:top',))):
            plan = select_stack(data, agents=('grok',), now=NOW, policy=policy)
            self.assertEqual(plan['selections']['grok']['canonical_id'], 'synthetic:other')

    def test_search_limit_never_returns_unproved_best_partial_stack(self):
        with self.assertRaises(PolicyError) as error:
            select_stack(bundle(), now=NOW, policy=SelectionPolicy(max_search_states=2))
        self.assertEqual(error.exception.code, 'selection_budget_exhausted')

    def test_provider_specific_argv_does_not_invent_cursor_effort_flag(self):
        plan = select_stack(bundle(), now=NOW)
        for agent, selection in plan['selections'].items():
            args = cli_selection_args(selection)
            self.assertEqual(args[:2], ('--model', selection['cli_model_id']))
            if agent == 'codex':
                self.assertEqual(args[2:], ('--config', 'model_reasoning_effort="high"'))
            elif agent == 'cursor':
                self.assertEqual(len(args), 2)
                self.assertTrue(selection['cli_model_id'].endswith('-high'))
            else:
                self.assertEqual(args[2:], ('--effort', 'high'))

    def test_only_proven_fixed_effort_can_omit_control(self):
        data = bundle({'cursor': [candidate('cursor', 'synthetic:fixed', levels=('not_configurable',))]})
        data['catalogs']['cursor']['candidates'][0]['effort_control'] = 'fixed'
        selected = select_stack(data, agents=('cursor',), now=NOW)['selections']['cursor']
        self.assertEqual(len(cli_selection_args(selected)), 2)
        data['catalogs']['cursor']['candidates'][0]['efforts'][0]['level'] = 'max'
        with self.assertRaises(PolicyError):
            select_stack(data, agents=('cursor',), now=NOW)

    def test_effort_variant_cannot_change_underlying_model_and_alias_pin_is_required(self):
        for change in ({'model_ref': 'synthetic:grok'}, {'cli_model_id': 'auto'}, {'pin_verified': False}):
            data = bundle()
            data['catalogs']['cursor']['candidates'][0]['efforts'][-1].update(change)
            with self.subTest(change=change), self.assertRaises(PolicyError):
                select_stack(data, now=NOW)

    def test_plan_expires_and_any_account_binary_or_catalog_change_invalidates(self):
        data = bundle()
        plan = select_stack(data, now=NOW)
        with self.assertRaises(PolicyError):
            validate_plan(plan, data, now=plan['expires_at'])
        for key in ('account_ref', 'cli_sha256', 'evidence_sha256'):
            changed = deepcopy(data)
            changed['catalogs']['codex'][key] = 'b' * 64
            with self.subTest(key=key), self.assertRaises(PolicyError):
                validate_plan(plan, changed, now=NOW)
        tampered = deepcopy(plan)
        tampered['selections']['codex']['effort'] = 'low'
        with self.assertRaises(PolicyError):
            validate_plan(tampered, data, now=NOW)
        for key in ('subscription_only', 'live_catalog_fetchers_implemented'):
            tampered = deepcopy(plan)
            tampered[key] = not tampered[key]
            with self.assertRaises(PolicyError):
                validate_plan(tampered, data, now=NOW)

    def test_actual_metadata_must_match_canonical_identity_and_effort(self):
        data = bundle()
        plan = select_stack(data, now=NOW)
        self.assertTrue(verify_observed_selection(plan, data, 'codex', model_ref='synthetic:codex', effort='high', now=NOW))
        for model, effort in (('synthetic:claude', 'high'), ('synthetic:codex', 'low'), ('auto', 'high')):
            with self.subTest(model=model, effort=effort), self.assertRaises(PolicyError):
                verify_observed_selection(plan, data, 'codex', model_ref=model, effort=effort, now=NOW)

    def test_evidence_size_and_invalid_limits_are_bounded(self):
        data = bundle()
        data['unexpected'] = 'x' * 256000
        with self.assertRaises(PolicyError):
            select_stack(data, now=NOW)
        for changes in ({'max_search_states': True}, {'max_candidates_per_agent': 1000}, {'max_plan_age_seconds': 0}):
            with self.assertRaises(PolicyError):
                replace(SelectionPolicy(), **changes)

    def test_claude_1m_context_variant_preserved_but_deduplicated_to_base_identity(self):
        row = candidate('claude', 'synthetic:claude-opus-5', levels=('high', 'max'))
        data = bundle({'claude': [row]})
        alias = 'synthetic:claude-opus-5[1m]'
        data['registry']['aliases'][alias] = 'synthetic:claude-opus-5'
        row['model_ref'] = alias
        for option in row['efforts']:
            option.update(cli_model_id='claude-opus-5[1m]', model_ref=alias)
        plan = select_stack(data, agents=('claude',), now=NOW)
        selected = plan['selections']['claude']
        self.assertEqual(selected['canonical_id'], 'synthetic:claude-opus-5')
        self.assertEqual(cli_selection_args(selected), ('--model', 'claude-opus-5[1m]', '--effort', 'max'))
        self.assertTrue(verify_observed_selection(plan, data, 'claude', model_ref=alias, effort='max', now=NOW))
        other = candidate('copilot', 'synthetic:claude-opus-5', levels=('high', 'max'))
        data['catalogs']['copilot'] = bundle({'copilot': [other]})['catalogs']['copilot']
        with self.assertRaises(PolicyError) as failure:
            select_stack(data, agents=('claude', 'copilot'), now=NOW)
        self.assertEqual(failure.exception.code, 'no_distinct_stack')

    def test_context_alias_cannot_change_identity_or_accept_unregistered_suffix(self):
        for suffix in ('[2m]', '[anything]', '[1m]'):
            data = bundle()
            for option in data['catalogs']['claude']['candidates'][0]['efforts']:
                option['cli_model_id'] += suffix
            with self.subTest(suffix=suffix), self.assertRaises(PolicyError):
                select_stack(data, now=NOW)
        data = bundle()
        data['registry']['aliases']['synthetic:claude[1m]'] = 'synthetic:copilot'
        with self.assertRaises(PolicyError) as failure:
            select_stack(data, now=NOW)
        self.assertEqual(failure.exception.code, 'context_variant_changes_model')

    def credit_catalog(self, rows):
        data = bundle(rows)
        for catalog in data['catalogs'].values():
            catalog['overage_disabled'] = False
            catalog['credit_controls'] = {
                'verified': True, 'provider_cap_enforced': True,
                'existing_balance_microusd': 2000000, 'remaining_spend_cap_microusd': 1000000,
                'auto_reload_enabled': False, 'automatic_purchase_enabled': False,
                'api_fallback_enabled': False, 'evidence_sha256': HASH, 'pool_ref': HASH}
        return data

    def test_included_usage_precedes_existing_credits_even_with_stronger_credit_model(self):
        included = candidate('claude', 'synthetic:included', 2)
        paid = candidate('claude', 'synthetic:credit', 1)
        paid['billing'] = 'existing_credits'
        data = self.credit_catalog({'claude': [included, paid]})
        plan = select_stack(data, agents=('claude',), now=NOW)
        self.assertEqual(plan['selections']['claude']['canonical_id'], 'synthetic:included')
        self.assertEqual(plan['selections']['claude']['billing'], 'subscription_included')
        self.assertEqual(plan['selections']['claude']['credit_enforcement'], 'provider_existing_balance_and_cap')
        self.assertEqual(plan['selections']['claude']['provider_credit_allowance_microusd'], 1000000)
        self.assertEqual(plan['existing_credit_agents'], 0)
        included['allowance'] = 'exhausted'
        paid_plan = select_stack(data, agents=('claude',), now=NOW)
        self.assertEqual(paid_plan['selections']['claude']['billing'], 'existing_credits')
        self.assertEqual(paid_plan['existing_credit_agents'], 1)

    def test_strict_subscription_only_never_allows_existing_credit_fallback(self):
        paid = candidate('claude', 'synthetic:credit')
        paid['billing'] = 'existing_credits'
        data = self.credit_catalog({'claude': [paid]})
        with self.assertRaises(PolicyError):
            select_stack(data, agents=('claude',), now=NOW, policy=SelectionPolicy(billing_policy='subscription_only'))
        strict = select_stack(bundle(), now=NOW, policy=SelectionPolicy(billing_policy='subscription_only'))
        self.assertTrue(strict['subscription_only'])

    def test_existing_credit_admission_requires_known_provider_controls_and_cap(self):
        for key, value in (
                ('auto_reload_enabled', True), ('automatic_purchase_enabled', True),
                ('api_fallback_enabled', True), ('provider_cap_enforced', False), ('verified', False),
                ('existing_balance_microusd', None), ('remaining_spend_cap_microusd', None),
                ('existing_balance_microusd', True), ('remaining_spend_cap_microusd', -1)):
            row = candidate('grok', 'synthetic:credit')
            row['billing'] = 'existing_credits'
            data = self.credit_catalog({'grok': [row]})
            data['catalogs']['grok']['credit_controls'][key] = value
            with self.subTest(key=key), self.assertRaises(PolicyError):
                select_stack(data, agents=('grok',), now=NOW)

    def test_provider_enforced_shared_cap_needs_no_invented_per_call_reservation(self):
        first, second = candidate('codex', 'synthetic:a'), candidate('copilot', 'synthetic:b')
        first['billing'] = second['billing'] = 'existing_credits'
        data = self.credit_catalog({'codex': [first], 'copilot': [second]})
        plan = select_stack(data, agents=('codex', 'copilot'), now=NOW)
        self.assertEqual(plan['existing_credit_agents'], 2)
        for selected in plan['selections'].values():
            self.assertEqual(selected['credit_pool_ref'], HASH)
            self.assertEqual(selected['provider_credit_allowance_microusd'], 1000000)
            self.assertEqual(selected['credit_enforcement'], 'provider_existing_balance_and_cap')
            self.assertNotIn('credit_reserve_microusd', selected)
        self.assertTrue(validate_plan(plan, data, now=NOW))

    def test_exhausted_provider_credit_cap_still_allows_known_included_usage_without_credit_hold(self):
        included, paid = candidate('claude', 'synthetic:a'), candidate('claude', 'synthetic:b', 2)
        paid['billing'] = 'existing_credits'
        data = self.credit_catalog({'claude': [included, paid]})
        data['catalogs']['claude']['credit_controls']['remaining_spend_cap_microusd'] = 0
        plan = select_stack(data, agents=('claude',), now=NOW)
        self.assertEqual(plan['selections']['claude']['billing'], 'subscription_included')
        self.assertEqual(plan['selections']['claude']['provider_credit_allowance_microusd'], 0)
        self.assertNotIn('credit_reserve_microusd', plan['selections']['claude'])


if __name__ == '__main__':
    unittest.main()
