"""Regression coverage for billing mistakes before a provider process starts."""
from dataclasses import asdict, replace
import json
import unittest

from agent_hub.billing_policy import (
    AuthEvidence, BillingPolicy, BillingPolicyError, enforce_billing_policy,
    evaluate_billing_policy, parse_billing_policy,
)


class BillingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.policy = BillingPolicy(mode='subscription_only')

    def evidence(self, provider, **changes):
        return replace(AuthEvidence(provider, 'subscription_login', self.now,
                                    'vendor_cli_status', True), **changes)

    def decide(self, provider='codex', environment=None, evidence=None, policy=None):
        return evaluate_billing_policy(provider, environment or {}, policy or self.policy,
                                       evidence, now=self.now)

    def test_legacy_default_does_not_require_auth_or_change_environment(self):
        environment = {'ANTHROPIC_API_KEY': 'private-fixture-value'}
        original = environment.copy()
        decision = self.decide('claude', environment, policy=parse_billing_policy())
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, 'policy_not_enabled')
        self.assertEqual(environment, original)
        self.assertEqual(decision.auth_status, 'unknown')
        self.assertIsNone(decision.remaining_allowance)

    def test_absent_credentials_do_not_become_proof_of_subscription(self):
        for provider in ('codex', 'claude', 'copilot', 'cursor', 'grok'):
            with self.subTest(provider=provider):
                self.assertFalse(self.decide(provider).allowed)
                self.assertFalse(self.decide(provider, evidence=self.evidence(
                    provider, auth_route='unknown')).allowed)

    def test_environment_api_and_custom_provider_overrides_block_even_with_good_evidence(self):
        cases = {
            'codex': ['OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL'],
            'claude': ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                       'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'],
            'copilot': ['COPILOT_PROVIDER_API_KEY', 'COPILOT_PROVIDER_BASE_URL',
                        'COPILOT_PROVIDER_BEARER_TOKEN', 'COPILOT_PROVIDER_API_KEY_COMMAND',
                        'COPILOT_PROVIDER_FUTURE_OVERRIDE'],
            'cursor': ['ANTHROPIC_API_KEY'],
            'grok': ['XAI_API_KEY', 'GROK_CLI_CHAT_PROXY_BASE_URL', 'GROK_AUTH_PROVIDER_COMMAND'],
        }
        for provider, keys in cases.items():
            for key in keys:
                for spelling in (key, key.lower()):
                    with self.subTest(provider=provider, key=spelling):
                        value = 'secret-value-that-must-never-appear'
                        environment = {spelling: value}
                        decision = self.decide(provider, environment, self.evidence(provider))
                        self.assertFalse(decision.allowed)
                        self.assertEqual(decision.code, 'provider_override')
                        self.assertNotIn(value, json.dumps(asdict(decision)))
                        with self.assertRaises(BillingPolicyError) as raised:
                            enforce_billing_policy(provider, environment, self.policy,
                                                   self.evidence(provider), now=self.now)
                        self.assertNotIn(value, str(raised.exception))
                        self.assertEqual(environment, {spelling: value})

    def test_github_account_tokens_are_distinct_from_byok_provider_keys(self):
        environment = {'COPILOT_GITHUB_TOKEN': 'account-token', 'GH_TOKEN': 'account-token',
                       'GITHUB_TOKEN': 'account-token'}
        self.assertTrue(self.decide('copilot', environment, self.evidence('copilot')).allowed)
        self.assertFalse(self.decide('copilot', environment).allowed)

    def test_claude_oauth_token_requires_account_route_evidence(self):
        environment = {'CLAUDE_CODE_OAUTH_TOKEN': 'account-token'}
        self.assertTrue(self.decide('claude', environment, self.evidence('claude')).allowed)
        self.assertFalse(self.decide('claude', environment).allowed)

    def test_api_key_from_stored_config_is_blocked_without_any_environment_override(self):
        for provider in ('codex', 'claude', 'copilot', 'cursor', 'grok'):
            with self.subTest(provider=provider):
                self.assertFalse(self.decide(provider, evidence=self.evidence(
                    provider, auth_route='provider_api_key')).allowed)

    def test_status_command_alone_cannot_clear_unchecked_effective_configuration(self):
        decision = self.decide(evidence=self.evidence('codex', local_configuration_checked=False))
        self.assertEqual(decision.code, 'unchecked_configuration')
        self.assertFalse(decision.allowed)

    def test_evidence_is_provider_bound_and_fresh(self):
        self.assertFalse(self.decide(evidence=self.evidence('claude')).allowed)
        for observed in (self.now - 301, self.now + 1, 0, float('nan'), float('inf'), True, 'now'):
            with self.subTest(observed=observed):
                decision = self.decide(evidence=self.evidence('codex', observed_at=observed))
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.code, 'stale_auth_evidence')
        self.assertTrue(self.decide(evidence=self.evidence('codex', observed_at=self.now - 300)).allowed)

    def test_malformed_provenance_or_boolean_fails_closed_without_echoing_input(self):
        for changes in ({'provenance': 'a-private-value'}, {'local_configuration_checked': 'true'},
                        {'auth_route': 'a-private-value'}, {'auth_route': []}, {'provenance': {}}):
            decision = self.decide(evidence=self.evidence('codex', **changes))
            self.assertFalse(decision.allowed)
            self.assertNotIn('a-private-value', repr(decision))

    def test_cursor_user_key_requires_both_distinct_evidence_and_explicit_enablement(self):
        environment = {'CURSOR_API_KEY': 'cursor-issued-key-fixture'}
        evidence = self.evidence('cursor', auth_route='cursor_user_key')
        self.assertFalse(self.decide('cursor', environment, evidence).allowed)
        policy = replace(self.policy, allow_cursor_user_key=True)
        self.assertFalse(self.decide('cursor', environment, self.evidence('cursor'), policy).allowed)
        decision = self.decide('cursor', environment, evidence, policy)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.observed_auth_route, 'cursor_user_key')
        self.assertEqual(decision.auth_status, 'unknown')
        self.assertIsNone(decision.remaining_allowance)
        self.assertEqual(decision.additional_usage_charges, 'unknown')
        self.assertNotIn(environment['CURSOR_API_KEY'], repr(decision))
        self.assertFalse(self.decide('codex', evidence=self.evidence(
            'codex', auth_route='cursor_user_key'), policy=policy).allowed)

    def test_accepted_account_route_retains_observation_without_claiming_live_validity(self):
        decision = self.decide(evidence=self.evidence('codex'))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.evidence_provenance, 'vendor_cli_status')
        self.assertEqual(decision.evidence_observed_at, self.now)
        self.assertEqual(decision.auth_status, 'unknown')
        self.assertIsNone(decision.remaining_allowance)
        self.assertEqual(decision.additional_usage_charges, 'unknown')

    def test_empty_unselected_key_does_not_disable_account_route(self):
        self.assertTrue(self.decide(environment={'OPENAI_API_KEY': ''},
                                    evidence=self.evidence('codex')).allowed)

    def test_invalid_policy_never_silently_becomes_legacy_mode(self):
        for value in ('subscription_only', False, {'mode': 'subscription'},
                      {'allow_cursor_user_key': 'true'}, {'mode': []},
                      {'max_evidence_age_seconds': True}, {'max_evidence_age_seconds': 0},
                      {'max_evidence_age_seconds': 3601}, {'subscription_only': True}):
            with self.subTest(value=value), self.assertRaises(BillingPolicyError):
                parse_billing_policy(value)


if __name__ == '__main__':
    unittest.main()
