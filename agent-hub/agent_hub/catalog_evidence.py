"""Assess a strongest-model upper bound without inventing a complete catalog.

Pure controller-side evidence validation. No IO, provider calls or inference.
Receipts are trusted collector assertions backed by immutable source artifacts;
SHA256s bind those artifacts but do not authenticate or independently prove them.
This module is NOT wired into model_policy or the worker launch path.
"""
from copy import deepcopy
import hashlib
import json
import re
import time

AGENTS = ('codex', 'claude', 'cursor', 'copilot', 'grok')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}')
_SHA = re.compile(r'[a-f0-9]{64}')
_EFFORT = re.compile(r'[a-z][a-z0-9_-]{0,31}')
MAX_BYTES = 64000


class EvidenceError(ValueError):
    pass


def _hash(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise EvidenceError('invalid_json_evidence') from None
    if len(raw) > MAX_BYTES:
        raise EvidenceError('evidence_size_limit')
    return hashlib.sha256(raw).hexdigest()


def _id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _fresh(proof, now, max_age):
    if not isinstance(proof, dict) or not _sha(proof.get('evidence_sha256')):
        return False
    observed, expiry = proof.get('observed_at'), proof.get('expires_at')
    return (type(observed) is int and type(expiry) is int and 0 <= observed <= now < expiry
            and 0 <= now - observed < max_age and expiry > observed)


def _bound(proof, receipt):
    return isinstance(proof, dict) and all(proof.get(k) == receipt[k]
        for k in ('agent', 'account_ref', 'cli_sha256', 'canonical_id', 'cli_model_id'))


def assess_frontier(receipt, *, expected_agent, expected_account_ref, expected_cli_sha256,
                    now=None, max_age_seconds=3600):
    """Return honest readiness/gaps for one candidate, preserving incomplete coverage.

    Expected bindings must come from independent protected deployment config.
    Scope/ranking/identity evidence is reviewed upstream; task text cannot supply
    these receipts. ``selection_eligible`` is evidence sufficiency, not permission
    to spend or to bypass the actual native launch/runtime gates.
    """
    now = int(time.time()) if now is None else now
    if (type(now) is not int or now < 0 or type(max_age_seconds) is not int
            or not 1 <= max_age_seconds <= 86400):
        raise EvidenceError('invalid_evidence_clock')
    if expected_agent not in AGENTS or not _sha(expected_account_ref) or not _sha(expected_cli_sha256):
        raise EvidenceError('independent_bindings_required')
    receipt_sha = _hash(receipt)
    if (not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int
            or receipt['schema_version'] != 1
            or any(receipt.get(k) != v for k, v in (
                ('agent', expected_agent), ('account_ref', expected_account_ref), ('cli_sha256', expected_cli_sha256)))
            or receipt.get('coverage') != 'provider_upper_bound'
            or receipt.get('complete_account_catalog') is not False):
        raise EvidenceError('invalid_frontier_or_binding')
    for key in ('canonical_id', 'family', 'cli_model_id', 'cli_version'):
        value = receipt.get(key)
        # Context selectors require explicit identity evidence; no suffix stripping.
        if not (_id(value) or key == 'cli_model_id' and isinstance(value, str)
                and value.endswith('[1m]') and _id(value[:-4])):
            raise EvidenceError('unresolved_candidate_identity')
    if ':' not in receipt['canonical_id'] or not _sha(receipt.get('identity_evidence_sha256')):
        raise EvidenceError('unresolved_candidate_identity')
    if receipt['cli_model_id'] in ('auto', 'best', 'default', 'latest', 'opus', 'sonnet', 'haiku', 'fable'):
        raise EvidenceError('exact_model_pin_required')
    gaps, accepted = [], []
    ranking = receipt.get('ranking')
    rank_ok = (_fresh(ranking, now, max_age_seconds)
        and ranking.get('kind') == 'official_provider_upper_bound'
        and ranking.get('agent') == expected_agent
        and ranking.get('canonical_id') == receipt['canonical_id']
        and ranking.get('ranking_scope') == 'general_coding_capability'
        and ranking.get('universe') == 'all_models_available_through_agent'
        and ranking.get('no_stronger_model') is True
        and isinstance(ranking.get('source_urls'), list) and 1 <= len(ranking['source_urls']) <= 8
        and all(isinstance(url, str) and url.startswith('https://') and len(url) <= 1024
                for url in ranking['source_urls']))
    if rank_ok:
        accepted.append(ranking)
    else:
        gaps.append('current_scoped_provider_upper_bound_required')
    native = receipt.get('native')
    native_ok = (_fresh(native, now, max_age_seconds) and _bound(native, receipt)
        and native.get('kind') == 'native_applied_settings'
        and native.get('auth_route') == 'first_party_subscription'
        and native.get('applied_model') == receipt['cli_model_id']
        and _sha(native.get('session_ref')) and _sha(native.get('effective_config_sha256'))
        and native.get('account_effective_order_verified') is True)
    efforts = native.get('effort_order') if isinstance(native, dict) else None
    native_ok = native_ok and (isinstance(efforts, list) and 1 <= len(efforts) <= 16
        and all(isinstance(x, str) and _EFFORT.fullmatch(x) for x in efforts)
        and len(set(efforts)) == len(efforts) and native.get('applied_effort') == efforts[-1])
    if native_ok:
        accepted.append(native)
    else:
        gaps.append('current_account_native_maximum_effort_required')
    entitlement = receipt.get('entitlement')
    entitlement_ok = (_fresh(entitlement, now, max_age_seconds) and _bound(entitlement, receipt)
        and entitlement.get('kind') == 'provider_completed_inference'
        and entitlement.get('accepted') is True
        and entitlement.get('served_canonical_id') == receipt['canonical_id']
        and entitlement.get('model_identity_source') == 'provider_runtime_metadata'
        and entitlement.get('fallback_occurred') is False
        and _sha(entitlement.get('request_binding_sha256'))
        and native_ok and entitlement.get('session_ref') == native['session_ref']
        and entitlement.get('effort') == native['applied_effort'])
    if entitlement_ok:
        accepted.append(entitlement)
    else:
        gaps.append('bounded_account_eligibility_result_required')
    billing = receipt.get('billing')
    billing_ok = (_fresh(billing, now, min(max_age_seconds, 300)) and _bound(billing, receipt)
        and billing.get('kind') == 'provider_current_billing_controls'
        and billing.get('provider_route_enforced') is True
        and billing.get('api_fallback_enabled') is False
        and billing.get('auto_reload_enabled') is False
        and billing.get('automatic_purchase_enabled') is False
        and billing.get('route') in ('subscription_included', 'existing_credits'))
    credit_ok = False
    if billing_ok:
        credits = billing.get('credit_controls')
        credit_ok = (isinstance(credits, dict) and credits.get('provider_cap_enforced') is True
            and credits.get('currency') == 'USD' and _sha(credits.get('pool_ref'))
            and _sha(credits.get('evidence_sha256'))
            and all(type(credits.get(k)) is int and 0 < credits[k] <= 1000000000000
                    for k in ('existing_balance_microusd', 'remaining_spend_cap_microusd')))
        if billing['route'] == 'subscription_included':
            billing_ok = billing.get('included_route_allowed') is True and (
                billing.get('paid_fallback') == 'disabled'
                or billing.get('paid_fallback') == 'verified_existing_credits' and credit_ok)
        else:
            billing_ok = credit_ok
        if entitlement_ok and entitlement.get('billing_route') != billing['route']:
            billing_ok = False
    if billing_ok:
        accepted.append(billing)
    else:
        gaps.append('current_model_billing_route_and_controls_required')
    # A paid top model can be eligible yet not policy-optimal: an omitted weaker
    # included model beats it under included-first policy. Need account-wide proof.
    optimal_cost = billing_ok and (billing['route'] == 'subscription_included'
        or billing.get('all_included_routes_exhausted') is True
        and billing.get('included_exhaustion_scope') == 'all_models_available_through_agent'
        and _sha(billing.get('included_exhaustion_evidence_sha256')))
    if billing_ok and not optimal_cost:
        gaps.append('included_first_frontier_not_proven')
    eligible = rank_ok and native_ok and entitlement_ok and billing_ok
    expires_at = min([p['expires_at'] for p in accepted] + [now + max_age_seconds])
    for p in accepted:
        age = min(max_age_seconds, 300) if p is billing else max_age_seconds
        expires_at = min(expires_at, p['observed_at'] + age)
    candidate = None
    if eligible:
        candidate = {key: receipt[key] for key in ('agent', 'account_ref', 'canonical_id', 'family',
                                                    'cli_model_id', 'cli_version', 'cli_sha256')}
        candidate.update({'effort': native['applied_effort'], 'effort_order': list(efforts),
                          'billing': billing['route'], 'strength_rank': 1})
    return {'schema_version': 1, 'coverage': 'provider_upper_bound', 'complete_account_catalog': False,
            'receipt_sha256': receipt_sha, 'observed_at': now, 'expires_at': expires_at,
            'selection_eligible': eligible, 'cost_optimality_proven': bool(optimal_cost),
            'native_settings_are_entitlement': False, 'candidate': candidate,
            'gaps': gaps, 'runtime_dispatch_authorized': False}


def certify_frontier_stack(receipts, *, expected_bindings, now=None):
    """Certify a supplied all-top stack only when it reaches absolute bounds.

    No search or weaker fallback. Any duplicate model, unproven included-first
    minimum, or missing maximum family diversity requires more evidence. Readiness
    does not mean proof of task-specific model quality or independent judgments.
    """
    now = int(time.time()) if now is None else now
    if (not isinstance(receipts, dict) or not 1 <= len(receipts) <= 5
            or not isinstance(expected_bindings, dict) or set(receipts) != set(expected_bindings)):
        raise EvidenceError('fleet_bindings_required')
    assessments, selections = {}, {}
    for agent in sorted(receipts):
        binding = expected_bindings[agent]
        if not isinstance(binding, dict):
            raise EvidenceError('fleet_bindings_required')
        report = assess_frontier(receipts[agent], expected_agent=agent,
            expected_account_ref=binding.get('account_ref'), expected_cli_sha256=binding.get('cli_sha256'), now=now)
        assessments[agent] = report
        if not report['selection_eligible'] or not report['cost_optimality_proven']:
            raise EvidenceError('frontier_evidence_incomplete:' + agent)
        selections[agent] = deepcopy(report['candidate'])
    if len({x['canonical_id'] for x in selections.values()}) != len(selections):
        raise EvidenceError('distinct_model_frontier_conflict')
    if len({x['family'] for x in selections.values()}) != len(selections):
        raise EvidenceError('diversity_upper_bound_not_attained')
    return {'schema_version': 1, 'proof_mode': 'attained_provider_upper_bounds',
            'created_at': now, 'expires_at': min(x['expires_at'] for x in assessments.values()),
            'selections': selections, 'receipt_sha256s': {a: x['receipt_sha256'] for a, x in assessments.items()},
            'distinct_canonical_models': len(selections), 'distinct_families': len(selections),
            'strength_loss_profile': [0] * len(selections), 'complete_account_catalog': False,
            'runtime_dispatch_authorized': False}
