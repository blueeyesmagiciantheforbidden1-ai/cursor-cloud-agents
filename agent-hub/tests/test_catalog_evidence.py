"""Synthetic offline receipt tests, not live provider or entitlement evidence."""
from copy import deepcopy
import unittest

from agent_hub.catalog_evidence import EvidenceError, assess_frontier, certify_frontier_stack

NOW = 10000


def fixture(agent='claude', provider='synthetic', family='synthetic-family'):
    binding = {'agent': agent, 'account_ref': 'a' * 64, 'cli_sha256': 'b' * 64,
               'canonical_id': provider + ':test-top', 'cli_model_id': 'test-top'}
    evidence = {'observed_at': NOW - 10, 'expires_at': NOW + 1000, 'evidence_sha256': 'c' * 64}
    record = {**binding, 'schema_version': 1, 'coverage': 'provider_upper_bound',
              'complete_account_catalog': False, 'family': family, 'cli_version': '1.0.0',
              'identity_evidence_sha256': 'd' * 64}
    record['ranking'] = {**evidence, 'kind': 'official_provider_upper_bound',
        'agent': agent, 'canonical_id': binding['canonical_id'], 'ranking_scope': 'general_coding_capability',
        'universe': 'all_models_available_through_agent', 'no_stronger_model': True,
        'source_urls': ['https://official-provider.invalid/models']}
    record['native'] = {**evidence, **binding, 'kind': 'native_applied_settings',
        'auth_route': 'first_party_subscription', 'applied_model': 'test-top',
        'session_ref': 'e' * 64, 'effective_config_sha256': 'f' * 64,
        'account_effective_order_verified': True, 'effort_order': ['high', 'maximum'], 'applied_effort': 'maximum'}
    record['entitlement'] = {**evidence, **binding, 'kind': 'provider_completed_inference',
        'accepted': True, 'served_canonical_id': binding['canonical_id'],
        'model_identity_source': 'provider_runtime_metadata', 'fallback_occurred': False,
        'request_binding_sha256': '1' * 64, 'session_ref': 'e' * 64, 'effort': 'maximum',
        'billing_route': 'subscription_included'}
    record['billing'] = {**evidence, **binding, 'kind': 'provider_current_billing_controls',
        'provider_route_enforced': True, 'api_fallback_enabled': False, 'auto_reload_enabled': False,
        'automatic_purchase_enabled': False, 'route': 'subscription_included',
        'included_route_allowed': True, 'paid_fallback': 'disabled'}
    return record


def assess(record):
    return assess_frontier(record, expected_agent=record['agent'], expected_account_ref='a' * 64,
                           expected_cli_sha256='b' * 64, now=NOW)


def credits(record, *, exhausted=False):
    record['billing'].update({'route': 'existing_credits', 'all_included_routes_exhausted': exhausted,
        'included_exhaustion_scope': 'all_models_available_through_agent',
        'included_exhaustion_evidence_sha256': '2' * 64,
        'credit_controls': {'currency': 'USD', 'provider_cap_enforced': True,
            'existing_balance_microusd': 100000000, 'remaining_spend_cap_microusd': 40000000,
            'pool_ref': '3' * 64, 'evidence_sha256': '4' * 64}})
    record['entitlement']['billing_route'] = 'existing_credits'


class FrontierTests(unittest.TestCase):
    def test_top_candidate_without_complete_catalog(self):
        report = assess(fixture())
        self.assertTrue(report['selection_eligible'])
        self.assertTrue(report['cost_optimality_proven'])
        self.assertFalse(report['complete_account_catalog'])
        self.assertFalse(report['runtime_dispatch_authorized'])
        self.assertEqual(report['candidate']['effort'], 'maximum')
        self.assertEqual(report['gaps'], [])

    def test_native_acceptance_not_server_entitlement(self):
        record = fixture()
        record['entitlement'] = None
        report = assess(record)
        self.assertFalse(report['selection_eligible'])
        self.assertFalse(report['native_settings_are_entitlement'])
        self.assertIn('bounded_account_eligibility_result_required', report['gaps'])

    def test_fake_complete_flag_rejected(self):
        record = fixture()
        record['complete_account_catalog'] = True
        with self.assertRaises(EvidenceError):
            assess(record)

    def test_independent_account_and_cli_bindings(self):
        for key in ('account_ref', 'cli_sha256'):
            record = fixture()
            record[key] = '0' * 64
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                assess(record)

    def test_provider_ranking_must_cover_agent_universe(self):
        record = fixture('cursor')
        record['ranking']['universe'] = 'only_one_provider_in_multi_provider_agent'
        report = assess(record)
        self.assertFalse(report['selection_eligible'])
        self.assertIn('current_scoped_provider_upper_bound_required', report['gaps'])

    def test_stale_receipt_future_time_and_billing_short_ttl(self):
        for proof, observed in [('ranking', NOW - 3600), ('native', NOW + 1), ('billing', NOW - 300)]:
            record = fixture()
            record[proof]['observed_at'] = observed
            with self.subTest(proof=proof):
                self.assertFalse(assess(record)['selection_eligible'])

    def test_low_effort_unknown_order_or_other_session_blocks(self):
        for path, value in [('applied_effort', 'high'), ('account_effective_order_verified', False),
                            ('session_ref', '5' * 64)]:
            record = fixture()
            record['native'][path] = value
            with self.subTest(path=path):
                self.assertFalse(assess(record)['selection_eligible'])

    def test_model_written_identity_and_fallback_do_not_prove_served_model(self):
        for key, value in [('model_identity_source', 'assistant_text'), ('fallback_occurred', True),
                           ('served_canonical_id', 'other:model')]:
            record = fixture()
            record['entitlement'][key] = value
            with self.subTest(key=key):
                self.assertFalse(assess(record)['selection_eligible'])

    def test_credit_model_eligible_but_not_included_first_optimal(self):
        record = fixture()
        credits(record)
        report = assess(record)
        self.assertTrue(report['selection_eligible'])
        self.assertFalse(report['cost_optimality_proven'])
        self.assertIn('included_first_frontier_not_proven', report['gaps'])

    def test_verified_global_included_exhaustion_supports_existing_credits(self):
        record = fixture()
        credits(record, exhausted=True)
        report = assess(record)
        self.assertTrue(report['selection_eligible'])
        self.assertTrue(report['cost_optimality_proven'])
        self.assertEqual(report['candidate']['billing'], 'existing_credits')

    def test_codex_units_unknown_balance_never_converted_to_currency(self):
        record = fixture('codex')
        credits(record, exhausted=True)
        record['billing']['credit_controls'] = {'hasCredits': True, 'balance': '250.0000000000'}
        self.assertFalse(assess(record)['selection_eligible'])

    def test_provider_cap_or_auto_purchase_unknown_blocks_credit(self):
        for key, value in [('auto_reload_enabled', True), ('automatic_purchase_enabled', None),
                           ('api_fallback_enabled', True)]:
            record = fixture()
            credits(record, exhausted=True)
            record['billing'][key] = value
            with self.subTest(key=key):
                self.assertFalse(assess(record)['selection_eligible'])
        record['billing']['credit_controls']['remaining_spend_cap_microusd'] = 0
        self.assertFalse(assess(record)['selection_eligible'])

    def test_selection_receipt_contains_no_grant_or_fake_catalog(self):
        record = fixture()
        report = assess(record)
        record['native']['applied_effort'] = 'high'
        self.assertEqual(report['candidate']['effort'], 'maximum')
        self.assertNotIn('complete', report)


class FrontierStackTests(unittest.TestCase):
    def certify(self, receipts):
        return certify_frontier_stack(receipts, expected_bindings={agent: {'account_ref': 'a' * 64,
            'cli_sha256': 'b' * 64} for agent in receipts}, now=NOW)

    def test_all_top_distinct_families_attains_bounds(self):
        result = self.certify({'claude': fixture(), 'codex': fixture('codex', 'other', 'other-family')})
        self.assertEqual(result['strength_loss_profile'], [0, 0])
        self.assertEqual(result['distinct_families'], 2)
        self.assertFalse(result['complete_account_catalog'])
        self.assertFalse(result['runtime_dispatch_authorized'])
        self.assertEqual(result['expires_at'], NOW + 290)

    def test_same_underlying_model_conflict_not_silently_downgraded(self):
        with self.assertRaisesRegex(EvidenceError, 'distinct_model_frontier_conflict'):
            self.certify({'claude': fixture(), 'cursor': fixture('cursor')})

    def test_missing_family_optimum_requires_more_evidence(self):
        with self.assertRaisesRegex(EvidenceError, 'diversity_upper_bound_not_attained'):
            self.certify({'claude': fixture(), 'cursor': fixture('cursor', 'other')})

    def test_credit_candidate_does_not_hide_weaker_included_option(self):
        record = fixture()
        credits(record)
        with self.assertRaisesRegex(EvidenceError, 'frontier_evidence_incomplete'):
            self.certify({'claude': record})

    def test_single_agent_requires_no_fake_other_catalogs(self):
        result = self.certify({'claude': fixture()})
        self.assertEqual(result['distinct_canonical_models'], 1)


if __name__ == '__main__':
    unittest.main()
