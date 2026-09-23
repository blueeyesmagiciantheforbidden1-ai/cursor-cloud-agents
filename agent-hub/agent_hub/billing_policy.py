"""Opt-in guard for provider account authentication, without reading credentials.

This module makes no network calls and never loads credential files. A trusted
local preflight must supply fresh, sanitized AuthEvidence. Evidence is an
observation, not an attestation: callers must check the selected executable,
its effective configuration (including workspace overrides and command flags),
and stored authentication before marking local_configuration_checked true.
Never accept that evidence from a hub task or a model response.

Evaluate the exact environment passed to Popen, immediately before spawning.
Passing this guard does NOT prove that a login remains valid, that allowance
remains, or that the provider has disabled account-level extra usage charges.
Those facts require separate provider evidence. Default mode preserves legacy
execution; subscription_only fails closed when routing evidence is missing.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import time


PROVIDERS = frozenset({'codex', 'claude', 'copilot', 'cursor', 'grok'})
AUTH_ROUTES = frozenset({'subscription_login', 'cursor_user_key', 'provider_api_key', 'unknown'})
PROVENANCES = frozenset({'vendor_cli_status', 'vendor_sdk_status', 'local_operator_audit'})

# Presence is checked case-insensitively for Windows and conservatively on Unix.
# Do not silently remove these: that could switch the user's selected account.
_OVERRIDES = {
    'codex': frozenset({
        'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL',
        'OPENAI_ORGANIZATION', 'OPENAI_ORG_ID', 'OPENAI_PROJECT_ID',
    }),
    'claude': frozenset({
        'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
        'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
    }),
    'cursor': frozenset({'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'XAI_API_KEY'}),
    'grok': frozenset({
        'XAI_API_KEY', 'GROK_API_KEY', 'GROK_CLI_CHAT_PROXY_BASE_URL',
        'GROK_AUTH_PROVIDER_COMMAND', 'GROK_OIDC_ISSUER', 'GROK_OIDC_CLIENT_ID',
    }),
}


class BillingPolicyError(ValueError):
    """A sanitized error; messages must never contain credential values."""


@dataclass(frozen=True)
class BillingPolicy:
    mode: str = 'provider_default'
    allow_cursor_user_key: bool = False
    max_evidence_age_seconds: int = 300

    def __post_init__(self):
        if self.mode not in ('provider_default', 'subscription_only'):
            raise BillingPolicyError('billing policy mode must be provider_default or subscription_only')
        if type(self.allow_cursor_user_key) is not bool:
            raise BillingPolicyError('allow_cursor_user_key must be true or false')
        if (type(self.max_evidence_age_seconds) is not int
                or not 1 <= self.max_evidence_age_seconds <= 3600):
            raise BillingPolicyError('max_evidence_age_seconds must be an integer from 1 to 3600')


def parse_billing_policy(value: object = None) -> BillingPolicy:
    """Parse the local worker's optional billing_policy object, never task data."""
    if value is None:
        return BillingPolicy()
    if not isinstance(value, dict) or set(value) - {
        'mode', 'allow_cursor_user_key', 'max_evidence_age_seconds'
    }:
        raise BillingPolicyError('billing_policy must be an object with supported policy settings')
    return BillingPolicy(**value)


@dataclass(frozen=True)
class AuthEvidence:
    """Sanitized local observation; contains no keys, tokens, or account IDs.

    observed_at is Unix time. configuration_checked includes any per-model API
    credentials and custom-provider settings, not only the default auth file.
    A vendor login status command alone generally does not establish this.
    """
    provider: str
    auth_route: str
    observed_at: float
    provenance: str
    local_configuration_checked: bool = False


@dataclass(frozen=True)
class BillingDecision:
    allowed: bool
    code: str
    message: str
    observed_auth_route: str = 'unknown'
    evidence_provenance: str | None = None
    evidence_observed_at: float | None = None
    # A routing check must not be converted into an account-health/credit claim.
    auth_status: str = 'unknown'
    remaining_allowance: None = None
    additional_usage_charges: str = 'unknown'


def _has_override(provider: str, environment: Mapping[str, str]) -> bool:
    for name, value in environment.items():
        key = name.upper()
        if not value:
            continue
        if provider == 'copilot':
            # Every BYOK provider knob routes outside Copilot account billing.
            # GitHub OAuth/PAT variables are account credentials, not BYOK API
            # keys, and are allowed only with fresh account-route evidence.
            if key.startswith('COPILOT_PROVIDER_'):
                return True
        elif key in _OVERRIDES[provider]:
            return True
    return False


def evaluate_billing_policy(
    provider: str,
    environment: Mapping[str, str],
    policy: BillingPolicy | None = None,
    evidence: AuthEvidence | None = None,
    *,
    now: float | None = None,
) -> BillingDecision:
    """Check a launch snapshot without modifying its environment or credentials.

    The evidence must describe this same launch configuration/environment.
    Credential rotation or a changed home/workspace/config requires new evidence.
    A Cursor user key is a separate account route; acceptance requires both
    allow_cursor_user_key and a local check that the key is Cursor-issued.
    Prefix matching a key is insufficient evidence of that fact.
    """
    policy = policy if policy is not None else BillingPolicy()
    if not isinstance(policy, BillingPolicy):
        raise BillingPolicyError('A validated BillingPolicy is required')
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise BillingPolicyError('Unsupported provider for billing policy')
    if policy.mode == 'provider_default':
        return BillingDecision(True, 'policy_not_enabled', 'Provider billing policy is not enforced.')
    if not isinstance(environment, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        return BillingDecision(False, 'invalid_environment', 'Cannot verify the child environment.')
    if _has_override(provider, environment):
        return BillingDecision(False, 'provider_override',
                               'Subscription-only execution blocked by a provider billing or routing override.')

    if (not isinstance(evidence, AuthEvidence) or evidence.provider != provider
            or not isinstance(evidence.auth_route, str) or evidence.auth_route not in AUTH_ROUTES
            or not isinstance(evidence.provenance, str) or evidence.provenance not in PROVENANCES
            or type(evidence.local_configuration_checked) is not bool):
        return BillingDecision(False, 'missing_auth_evidence',
                               'Subscription-only execution requires a local authentication-route check.')
    current = time.time() if now is None else now
    if (type(current) not in (int, float) or not math.isfinite(current)
            or type(evidence.observed_at) not in (int, float)
            or not math.isfinite(evidence.observed_at)
            or evidence.observed_at <= 0
            or not 0 <= current - evidence.observed_at <= policy.max_evidence_age_seconds):
        return BillingDecision(False, 'stale_auth_evidence',
                               'Subscription-only execution requires a fresh local authentication-route check.')
    if not evidence.local_configuration_checked:
        return BillingDecision(False, 'unchecked_configuration',
                               'Effective provider configuration has not been checked for billing overrides.')
    if evidence.auth_route in ('unknown', 'provider_api_key'):
        return BillingDecision(False, 'unapproved_auth_route',
                               'The observed authentication route is not approved for subscription-only execution.')

    cursor_key_present = any(name.upper() == 'CURSOR_API_KEY' and bool(value)
                             for name, value in environment.items())
    if evidence.auth_route == 'cursor_user_key':
        if provider != 'cursor' or not policy.allow_cursor_user_key:
            return BillingDecision(False, 'cursor_user_key_not_enabled',
                                   'The distinct Cursor user-key account route has not been enabled.')
        message = ('Cursor user-key account route accepted; login validity, included allowance, '
                   'and additional usage charges remain unverified.')
    else:
        if provider == 'cursor' and cursor_key_present:
            return BillingDecision(False, 'cursor_key_evidence_mismatch',
                                   'A Cursor key is present but was not checked as a Cursor-issued user key.')
        message = ('Provider account-login route accepted; login validity, included allowance, '
                   'and additional usage charges remain unverified.')
    return BillingDecision(True, 'account_route_accepted', message, evidence.auth_route,
                           evidence.provenance, float(evidence.observed_at))


def enforce_billing_policy(*args, **kwargs) -> BillingDecision:
    """Raise a sanitized exception before spawning when the routing gate fails."""
    decision = evaluate_billing_policy(*args, **kwargs)
    if not decision.allowed:
        raise BillingPolicyError(decision.message)
    return decision
