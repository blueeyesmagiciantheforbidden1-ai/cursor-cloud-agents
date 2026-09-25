"""Build hub-schema usage entries from MEASURED provider values only.

Stdlib only. Never invents balances, percentages-as-credits, or API-equivalent costs.
"""
from __future__ import annotations

import math
from datetime import datetime


def _nonneg_int(value):
    return type(value) is int and 0 <= value <= 10**15


def _finite_nonneg(value):
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1e15


def _session_token_used(provider, reply_usage):
    """Return input+output token count, or None when not both measured ints."""
    if not isinstance(reply_usage, dict):
        return None
    if provider == 'claude':
        nested = reply_usage.get('usage')
        if not isinstance(nested, dict):
            return None
        inp, out = nested.get('input_tokens'), nested.get('output_tokens')
    elif provider in ('codex', 'copilot', 'grok'):
        inp, out = reply_usage.get('inputTokens'), reply_usage.get('outputTokens')
    else:
        return None
    if not (_nonneg_int(inp) and _nonneg_int(out)):
        return None
    return inp + out


def _normalize_observed_at(value):
    """Timezone-aware ISO string normalised to a Z suffix, else None."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            return None
        return date.isoformat().replace('+00:00', 'Z')
    except (ValueError, OverflowError, TypeError):
        return None


def _premium_pool(preflight):
    """Locate the premium_interactions pool on a Copilot preflight quota blob."""
    if not isinstance(preflight, dict):
        return None
    quota = preflight.get('quota')
    if not isinstance(quota, dict):
        return None
    pools = quota.get('pools')
    if isinstance(pools, list):
        for pool in pools:
            if isinstance(pool, dict) and pool.get('quota_type') == 'premium_interactions':
                return pool
    snapshots = quota.get('quotaSnapshots')
    if isinstance(snapshots, dict):
        item = snapshots.get('premium_interactions')
        if isinstance(item, dict):
            return item
    return None


def _quota_observed_at(preflight):
    """Copilot quota is stamped at preflight; use that time, not the turn's."""
    if not isinstance(preflight, dict):
        return None
    quota = preflight.get('quota')
    if not isinstance(quota, dict):
        return None
    return _normalize_observed_at(quota.get('observed_at'))


def _copilot_account_requests(preflight):
    """Account premium request counters when measured and finite."""
    observed_at = _quota_observed_at(preflight)
    if observed_at is None:
        return None
    pool = _premium_pool(preflight)
    if not isinstance(pool, dict):
        return None
    if pool.get('isUnlimitedEntitlement') is True:
        return None
    used = pool.get('usedRequests')
    entitlement = pool.get('entitlementRequests')
    if not _finite_nonneg(used):
        return None
    limit = None
    if entitlement is not None:
        if not (type(entitlement) in (int, float) and not isinstance(entitlement, bool)
                and math.isfinite(entitlement)):
            return None
        if entitlement >= 0:
            limit = entitlement
    entry = {
        'scope': 'account',
        'metric': 'requests',
        'status': 'available',
        'source': 'provider_cli',
        'observed_at': observed_at,
        'used': used,
    }
    if limit is not None:
        entry['limit'] = limit
        remaining = limit - used
        if _finite_nonneg(remaining):
            entry['remaining'] = remaining
    return entry


def entries(provider, reply_usage, preflight, observed_at):
    """Return at most 8 hub-schema usage dicts from MEASURED values only.

    Never raises: unexpected shapes yield whatever is valid so far (possibly []).
    """
    out = []
    try:
        if not isinstance(provider, str):
            return []
        used = _session_token_used(provider, reply_usage)
        if used is not None and observed_at is not None:
            out.append({
                'scope': 'session',
                'metric': 'tokens',
                'status': 'available',
                'source': 'provider_cli',
                'observed_at': observed_at,
                'used': used,
            })
        if provider == 'copilot':
            account = _copilot_account_requests(preflight)
            if account is not None:
                out.append(account)
    except Exception:
        return out[:8]
    return out[:8]
