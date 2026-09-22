"""Pure, fail-closed model selection from trusted, fresh account evidence.

This module fetches no catalogs, changes no account settings, and runs no models.
Ranking evidence is an input: model names and release dates are not quality scores.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import time


AGENTS = ('codex', 'claude', 'cursor', 'copilot', 'grok')
VERSION = 'distinct-approved-allowances-v2'
MAX_BUNDLE_BYTES = 256000
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}')
_DIGEST = re.compile(r'[a-f0-9]{64}')
_EFFORT = re.compile(r'[a-z][a-z0-9_-]{0,31}')
_AUTO = frozenset(('auto', 'auto-smart', 'default', 'best', 'opus', 'sonnet', 'haiku', 'opusplan', 'latest'))
_CONTROLS = {'codex': ('codex-config', 'fixed'), 'claude': ('claude-effort', 'fixed'),
             'cursor': ('model-variant', 'fixed'), 'copilot': ('copilot-effort', 'fixed'),
             'grok': ('grok-effort', 'fixed')}


class PolicyError(ValueError):
    def __init__(self, code, agent=None):
        super().__init__(code + (':' + agent if agent in AGENTS else ''))
        self.code, self.agent = code, agent


@dataclass(frozen=True)
class SelectionPolicy:
    billing_policy: str = 'included_then_existing_credits'
    max_catalog_age_seconds: int = 3600
    max_plan_age_seconds: int = 300
    max_candidates_per_agent: int = 24
    max_search_states: int = 100000
    excluded_canonical_ids: tuple = ()
    excluded_families: tuple = ()

    def __post_init__(self):
        if self.billing_policy not in ('subscription_only', 'included_then_existing_credits'):
            raise PolicyError('invalid_billing_policy')
        for value, maximum in ((self.max_catalog_age_seconds, 86400), (self.max_plan_age_seconds, 3600),
                               (self.max_candidates_per_agent, 32), (self.max_search_states, 1000000)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise PolicyError('invalid_policy_limits')
        for values in (self.excluded_canonical_ids, self.excluded_families):
            if (not isinstance(values, tuple) or len(values) > 128
                    or any(not _identifier(value) for value in values) or len(values) != len(set(values))):
                raise PolicyError('invalid_policy_exclusions')


def _identifier(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _model_reference(value):
    # Claude's documented/observed extended-context selector is an execution
    # variant, not another underlying model. No arbitrary suffix stripping.
    return _identifier(value) or (isinstance(value, str) and value.endswith('[1m]') and _identifier(value[:-4]))


def _cli_model(value):
    base = value[:-4] if isinstance(value, str) and value.endswith('[1m]') else value
    return _model_reference(value) and base not in _AUTO and not base.endswith('-latest')


def _hash(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise PolicyError('invalid_json_evidence') from None
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise PolicyError('evidence_too_large')
    return hashlib.sha256(encoded).hexdigest()


def _evidence(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise PolicyError('missing_evidence_digest')


def _fresh(record, now, policy):
    if not isinstance(record, dict):
        raise PolicyError('missing_catalog')
    observed, expiry = record.get('observed_at'), record.get('expires_at')
    if (type(observed) is not int or type(expiry) is not int or observed < 0 or observed > now
            or expiry <= now or expiry <= observed or now - observed >= policy.max_catalog_age_seconds):
        raise PolicyError('stale_or_invalid_catalog')
    if not _identifier(record.get('revision')):
        raise PolicyError('missing_catalog_revision')
    _evidence(record.get('evidence_sha256'))
    return min(expiry, observed + policy.max_catalog_age_seconds)


def _registry(bundle, now, policy):
    registry = bundle.get('registry')
    expiry = _fresh(registry, now, policy)
    models, aliases = registry.get('models'), registry.get('aliases')
    if (not isinstance(models, dict) or not 1 <= len(models) <= 256
            or not isinstance(aliases, dict) or len(aliases) > 512 or set(models) & set(aliases)):
        raise PolicyError('invalid_identity_registry')
    for canonical, info in models.items():
        if (not _identifier(canonical) or ':' not in canonical or not isinstance(info, dict)
                or not _identifier(info.get('family'))):
            raise PolicyError('invalid_identity_registry')
        _evidence(info.get('identity_evidence_sha256'))
    for alias, target in aliases.items():
        if not _model_reference(alias) or not _model_reference(target):
            raise PolicyError('invalid_identity_registry')

    def resolve(reference):
        if not _model_reference(reference):
            raise PolicyError('unresolved_model_identity')
        seen = set()
        while reference in aliases:
            if reference in seen:
                raise PolicyError('cyclic_model_alias')
            seen.add(reference)
            reference = aliases[reference]
        if reference not in models:
            raise PolicyError('unresolved_model_identity')
        return reference
    # Invalid aliases may conceal duplicated underlying models: reject the registry.
    for alias in aliases:
        resolve(alias)
        if alias.endswith('[1m]') and resolve(alias) != resolve(alias[:-4]):
            raise PolicyError('context_variant_changes_model')
    return models, resolve, expiry


def _eligible(agent, catalog, models, resolve, policy):
    if catalog.get('complete') is not True or catalog.get('source') != 'account_catalog':
        raise PolicyError('incomplete_account_catalog', agent)
    if catalog.get('auth_route') != 'subscription':
        raise PolicyError('subscription_cost_enforcement_unverified', agent)
    credits = None
    if catalog.get('overage_disabled') is not True:
        if policy.billing_policy == 'subscription_only':
            raise PolicyError('subscription_cost_enforcement_unverified', agent)
        credits = catalog.get('credit_controls')
        if (not isinstance(credits, dict) or credits.get('verified') is not True
                or credits.get('provider_cap_enforced') is not True
                or any(credits.get(key) is not False for key in
                       ('auto_reload_enabled', 'automatic_purchase_enabled', 'api_fallback_enabled'))):
            raise PolicyError('existing_credit_controls_unverified', agent)
        for key in ('existing_balance_microusd', 'remaining_spend_cap_microusd'):
            if type(credits.get(key)) is not int or not 0 <= credits[key] <= 1000000000000:
                raise PolicyError('existing_credit_limits_unknown', agent)
        _evidence(credits.get('evidence_sha256'))
        _evidence(credits.get('pool_ref'))
    for name in ('account_ref', 'cli_sha256', 'ranking_evidence_sha256'):
        _evidence(catalog.get(name))
    if not _identifier(catalog.get('cli_version')):
        raise PolicyError('unknown_cli_version', agent)
    candidates = catalog.get('candidates')
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= policy.max_candidates_per_agent:
        raise PolicyError('invalid_candidate_count', agent)
    selected, excluded = {}, []
    for row in candidates:
        if not isinstance(row, dict):
            raise PolicyError('invalid_candidate', agent)
        accessible, billing, allowance = row.get('accessible'), row.get('billing'), row.get('allowance')
        if billing == 'existing_credits' and (policy.billing_policy == 'subscription_only' or credits is None):
            excluded.append({'reason': 'existing_credits_not_enabled'})
            continue
        if accessible is False or billing in ('api_metered', 'requires_overage') or allowance == 'exhausted':
            excluded.append({'reason': 'unavailable_or_cost_excluded'})
            continue
        if accessible is not True or billing not in ('subscription_included', 'existing_credits') or allowance != 'available':
            raise PolicyError('unknown_access_or_cost', agent)
        credit_limit, credit_pool = 0, None
        if credits is not None:
            credit_limit = min(credits['existing_balance_microusd'], credits['remaining_spend_cap_microusd'])
            credit_pool = credits['pool_ref']
            if credit_limit == 0:
                if billing == 'existing_credits':
                    excluded.append({'reason': 'existing_credits_exhausted'})
                    continue
                # An enforced exhausted cap cannot automatically bill credits.
        canonical = resolve(row.get('model_ref'))
        family = models[canonical]['family']
        if canonical in policy.excluded_canonical_ids or family in policy.excluded_families:
            excluded.append({'canonical_id': canonical, 'reason': 'policy_excluded'})
            continue
        rank = row.get('strength_rank')
        if type(rank) is not int or not 1 <= rank <= 10000:
            raise PolicyError('unknown_strength_rank', agent)
        control, efforts = row.get('effort_control'), row.get('efforts')
        if control not in _CONTROLS[agent] or row.get('effective_capabilities_verified') is not True:
            raise PolicyError('unverified_cli_capabilities', agent)
        _evidence(row.get('capability_evidence_sha256'))
        if not isinstance(efforts, list) or not 1 <= len(efforts) <= 16:
            raise PolicyError('unknown_effort_support', agent)
        levels = []
        for option in efforts:
            if not isinstance(option, dict):
                raise PolicyError('unknown_effort_support', agent)
            level, cli_model = option.get('level'), option.get('cli_model_id')
            if (not isinstance(level, str) or not _EFFORT.fullmatch(level)
                    or not _cli_model(cli_model)
                    or option.get('pin_verified') is not True):
                raise PolicyError('unresolved_cli_model_or_effort', agent)
            if resolve(option.get('model_ref')) != canonical:
                raise PolicyError('effort_changes_model', agent)
            if cli_model.endswith('[1m]') and not option['model_ref'].endswith('[1m]'):
                raise PolicyError('unbound_context_variant', agent)
            levels.append(level)
        if len(levels) != len(set(levels)):
            raise PolicyError('ambiguous_effort_order', agent)
        if control == 'fixed' and levels != ['not_configurable']:
            raise PolicyError('unknown_fixed_effort', agent)
        if control != 'fixed' and 'not_configurable' in levels:
            raise PolicyError('unknown_effort_support', agent)
        if control not in ('model-variant', 'fixed') and len({item['cli_model_id'] for item in efforts}) != 1:
            raise PolicyError('effort_changes_model', agent)
        # The collector supplies the evidenced least-to-most effort order. Never
        # infer semantics from spelling or append a universal "max" flag.
        highest = efforts[-1]
        value = {'agent': agent, 'canonical_id': canonical, 'family': family, 'strength_rank': rank,
                 'cli_model_id': highest['cli_model_id'], 'effort': highest['level'], 'effort_control': control,
                 'account_ref': catalog['account_ref'], 'cli_version': catalog['cli_version'],
                 'cli_sha256': catalog['cli_sha256'], 'catalog_revision': catalog['revision'],
                 'capability_evidence_sha256': row['capability_evidence_sha256'],
                 'effort_order': levels, 'billing': billing,
                 'credit_enforcement': ('provider_existing_balance_and_cap' if credits is not None
                                        else 'provider_disabled'),
                 'credit_pool_ref': credit_pool,
                 'provider_credit_allowance_microusd': credit_limit}
        if canonical in selected:
            previous = selected[canonical]
            if any(previous[key] != value[key] for key in ('strength_rank', 'effort_order', 'effort_control',
                                                           'credit_enforcement', 'credit_pool_ref')):
                raise PolicyError('conflicting_alias_capabilities', agent)
            if (value['billing'] != 'subscription_included', value['cli_model_id']) < (
                    previous['billing'] != 'subscription_included', previous['cli_model_id']):
                selected[canonical] = value
        else:
            selected[canonical] = value
    if not selected:
        raise PolicyError('no_eligible_model', agent)
    ranks = sorted({value['strength_rank'] for value in selected.values()})
    for value in selected.values():
        value['strength_loss_levels'] = ranks.index(value['strength_rank'])
    return sorted(selected.values(), key=lambda item: (item['billing'] != 'subscription_included',
                                                       item['strength_rank'], item['canonical_id'])), excluded


def select_stack(bundle, *, agents=AGENTS, now=None, policy=None):
    """Return an optimal distinct stack under declared ranks, or no plan at all.

    Objective: prefer included usage, minimize sorted worst-first ordinal
    strength losses, then maximize family diversity with deterministic ties.
    """
    policy = policy or SelectionPolicy()
    if type(policy) is not SelectionPolicy:
        raise PolicyError('invalid_policy')
    now = int(time.time()) if now is None else now
    if type(now) is not int or now < 0:
        raise PolicyError('invalid_clock')
    if (not isinstance(agents, (list, tuple)) or not 1 <= len(agents) <= len(AGENTS)
            or any(agent not in AGENTS for agent in agents) or len(agents) != len(set(agents))):
        raise PolicyError('invalid_agents')
    agents = tuple(sorted(agents))
    if not isinstance(bundle, dict) or type(bundle.get('schema_version')) is not int or bundle.get('schema_version') != 1:
        raise PolicyError('invalid_bundle')
    bundle_hash = _hash(bundle)
    models, resolve, expiry = _registry(bundle, now, policy)
    catalogs = bundle.get('catalogs')
    if not isinstance(catalogs, dict):
        raise PolicyError('missing_catalog')
    options, exclusions = {}, {}
    for agent in agents:
        catalog = catalogs.get(agent)
        expiry = min(expiry, _fresh(catalog, now, policy))
        options[agent], exclusions[agent] = _eligible(agent, catalog, models, resolve, policy)
    search_order = sorted(agents, key=lambda agent: (len(options[agent]), agent))
    best, best_key, visited = None, None, 0

    def search(index, chosen, used):
        nonlocal best, best_key, visited
        if index == len(search_order):
            values = [chosen[agent] for agent in agents]
            key = (sum(value['billing'] == 'existing_credits' for value in values),
                   tuple(sorted((value['strength_loss_levels'] for value in values), reverse=True)),
                   -len({value['family'] for value in values}), tuple(value['canonical_id'] for value in values))
            if best_key is None or key < best_key:
                best, best_key = dict(chosen), key
            return
        optimistic = (sum(value['billing'] == 'existing_credits' for value in chosen.values()),
                      tuple(sorted([value['strength_loss_levels'] for value in chosen.values()] +
                                   [0] * (len(agents) - len(chosen)), reverse=True)))
        if best_key is not None and optimistic > best_key[:2]:
            return
        agent = search_order[index]
        for candidate in options[agent]:
            visited += 1
            if visited > policy.max_search_states:
                raise PolicyError('selection_budget_exhausted')
            identity = candidate['canonical_id']
            if identity in used:
                continue
            chosen[agent] = candidate
            search(index + 1, chosen, used | {identity})
            del chosen[agent]
    search(0, {}, set())
    if best is None:
        raise PolicyError('no_distinct_stack')
    selections = {agent: best[agent] for agent in agents}
    policy_hash = _hash(asdict(policy))
    digest = _hash({'version': VERSION, 'bundle_sha256': bundle_hash, 'policy_sha256': policy_hash,
                    'selections': selections})
    return {'schema_version': 1, 'policy_version': VERSION, 'bundle_sha256': bundle_hash,
            'policy_sha256': policy_hash, 'selection_sha256': digest, 'created_at': now,
            'expires_at': min(expiry, now + policy.max_plan_age_seconds), 'selections': selections,
            'distinct_canonical_models': len(selections), 'distinct_families': -best_key[2],
            'strength_loss_profile': list(best_key[1]), 'search_states': visited,
            'exclusions': exclusions, 'billing_policy': policy.billing_policy,
            'existing_credit_agents': best_key[0], 'api_billing_allowed': False,
            'subscription_only': policy.billing_policy == 'subscription_only',
            'live_catalog_fetchers_implemented': False}


def cli_selection_args(selection):
    """Provider-specific argv suffix. Caller must first validate the whole plan."""
    if not isinstance(selection, dict):
        raise PolicyError('invalid_selection')
    agent, model, effort, control = (selection.get(key) for key in ('agent', 'cli_model_id', 'effort', 'effort_control'))
    if (agent not in AGENTS or not _cli_model(model)
            or not isinstance(effort, str) or not _EFFORT.fullmatch(effort) or control not in _CONTROLS[agent]):
        raise PolicyError('invalid_selection')
    if control == 'fixed':
        if effort != 'not_configurable':
            raise PolicyError('invalid_selection')
        return ('--model', model)
    if control == 'model-variant':
        return ('--model', model)
    if control == 'codex-config':
        return ('--model', model, '--config', 'model_reasoning_effort=' + json.dumps(effort))
    return ('--model', model, '--effort', effort)


def validate_plan(plan, bundle, *, now=None, policy=None):
    """Recheck freshness, optimality, aliases, account/CLI bindings and tampering."""
    now = int(time.time()) if now is None else now
    if (not isinstance(plan, dict) or not isinstance(plan.get('selections'), dict)
            or type(now) is not int or type(plan.get('created_at')) is not int
            or type(plan.get('expires_at')) is not int or not plan['created_at'] <= now < plan['expires_at']):
        raise PolicyError('expired_or_invalid_plan')
    current = select_stack(bundle, agents=tuple(plan['selections']), now=now, policy=policy)
    for key in current:
        if key in ('created_at', 'expires_at'):
            continue
        if current[key] != plan.get(key):
            raise PolicyError('model_plan_changed')
    # Do not allow an untrusted timestamp extension to bypass the original TTL.
    active_policy = policy or SelectionPolicy()
    if plan['expires_at'] > min(current['expires_at'], plan['created_at'] + active_policy.max_plan_age_seconds):
        raise PolicyError('expired_or_invalid_plan')
    return True


def verify_observed_selection(plan, bundle, agent, *, model_ref, effort, now=None, policy=None):
    """Compare trusted CLI/provider metadata, never model-written response text."""
    validate_plan(plan, bundle, now=now, policy=policy)
    if agent not in plan['selections']:
        raise PolicyError('agent_not_selected')
    active_policy = policy or SelectionPolicy()
    observed_now = int(time.time()) if now is None else now
    _, resolve, _ = _registry(bundle, observed_now, active_policy)
    chosen = plan['selections'][agent]
    if resolve(model_ref) != chosen['canonical_id'] or effort != chosen['effort']:
        raise PolicyError('observed_model_or_effort_mismatch', agent)
    return True
