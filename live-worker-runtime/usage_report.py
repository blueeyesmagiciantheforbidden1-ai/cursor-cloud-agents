"""Build hub-schema usage entries from MEASURED provider values only.

Stdlib only. Never invents balances, percentages-as-credits, or API-equivalent costs.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


def _nonneg_int(value):
    return type(value) is int and 0 <= value <= 10**15


def _finite_nonneg(value):
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1e15


def _finite_percent(value):
    """True when value is a finite used-percent in [0, 100]."""
    return (type(value) in (int, float) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 100)


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


def _parse_aware(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, OverflowError, TypeError):
        return None
    if date.tzinfo is None:
        return None
    return date


def _valid_resets_at(value, observed_at):
    """Provider reset time as Z-ISO when later than observed_at and <= 400 days ahead."""
    reset = _normalize_observed_at(value)
    if reset is None or not isinstance(observed_at, str):
        return None
    reset_dt, obs_dt = _parse_aware(reset), _parse_aware(observed_at)
    if reset_dt is None or obs_dt is None:
        return None
    if reset_dt <= obs_dt:
        return None
    if reset_dt > obs_dt + timedelta(days=400):
        return None
    return reset


def _resets_at_from_epoch(epoch, observed_at):
    if type(epoch) is not int:
        return None
    try:
        reset = datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')
    except (OverflowError, OSError, ValueError):
        return None
    return _valid_resets_at(reset, observed_at)


def _window_minutes(value):
    """Return a hub window_minutes int, or None to omit (never guess)."""
    if value is None:
        return None
    if type(value) is int and 1 <= value <= 525600:
        return value
    return None


def _quota_percent_row(*, used, observed_at, window_minutes=None, resets_at=None):
    """Build one account/quota_percent row; both used and remaining required."""
    if not _finite_percent(used) or not isinstance(observed_at, str):
        return None
    remaining = 100 - used
    if not _finite_percent(remaining) or abs(used + remaining - 100) >= 1e-6:
        return None
    entry = {
        'scope': 'account',
        'metric': 'quota_percent',
        'status': 'available',
        'source': 'provider_cli',
        'observed_at': observed_at,
        'used': used,
        'remaining': remaining,
        'limit': 100,
    }
    minutes = _window_minutes(window_minutes)
    if minutes is not None:
        entry['window_minutes'] = minutes
    if resets_at is not None:
        entry['resets_at'] = resets_at
    return entry


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
    # No window_minutes on Copilot requests. resets_at only when provider resetDate is valid.
    resets_at = _valid_resets_at(pool.get('resetDate'), observed_at)
    if resets_at is not None:
        entry['resets_at'] = resets_at
    return entry


def _codex_measurement_time(preflight, quota):
    """Quota stamp if present, else preflight stamp; never the turn time."""
    if isinstance(quota, dict):
        stamped = _normalize_observed_at(quota.get('observed_at'))
        if stamped is not None:
            return stamped
    if isinstance(preflight, dict):
        return _normalize_observed_at(preflight.get('observed_at'))
    return None


def _codex_window_duration(window):
    """Prefer normalised window_duration_mins; accept raw windowDurationMins if present."""
    if not isinstance(window, dict):
        return None
    if 'window_duration_mins' in window:
        return _window_minutes(window.get('window_duration_mins'))
    return _window_minutes(window.get('windowDurationMins'))


def _codex_quota_percent_rows(preflight):
    """One quota_percent row per codex window that has a valid duration (1..525600).

    Windows without a valid duration emit nothing: no collapse to another window and
    no null-window row (that would shadow a distinct window_minutes identity).
    """
    if not isinstance(preflight, dict):
        return []
    quota = preflight.get('quota')
    if not isinstance(quota, dict):
        return []
    observed_at = _codex_measurement_time(preflight, quota)
    if observed_at is None:
        return []
    windows = quota.get('windows')
    if not isinstance(windows, list):
        return []
    best = {}
    for window in windows:
        if not isinstance(window, dict):
            continue
        if window.get('limit_id') != 'codex':
            continue
        used = window.get('used_percent')
        if used is None:
            continue
        minutes = _codex_window_duration(window)
        if minutes is None:
            continue
        resets_at = _resets_at_from_epoch(window.get('resets_at'), observed_at)
        row = _quota_percent_row(
            used=used, observed_at=observed_at,
            window_minutes=minutes, resets_at=resets_at)
        if row is None:
            continue
        key = row['window_minutes']
        previous = best.get(key)
        if previous is None or row['used'] > previous['used']:
            best[key] = row
    return list(best.values())


def _grok_quota_percent_row(preflight):
    """creditUsagePercent (as native_included_used_percent) → quota_percent; no guessed window."""
    if not isinstance(preflight, dict):
        return None
    quota = preflight.get('quota')
    if not isinstance(quota, dict):
        return None
    observed_at = _normalize_observed_at(quota.get('observed_at'))
    if observed_at is None:
        return None
    used = quota.get('native_included_used_percent')
    if used is None:
        return None
    resets_at = None
    period = quota.get('period')
    if isinstance(period, dict):
        resets_at = _valid_resets_at(period.get('end'), observed_at)
    # window_minutes omitted: Grok billing does not state a period length in minutes.
    # Legacy cents → budget_usd: omitted. providers/grok.py::_billing names the unit
    # via _cents / on_demand_*_cents / prepaid_balance_cents and test_grok.py
    # (monthlyLimit={'val':'100'}, used={'val':20} → usage_source native_legacy_cents),
    # but _billing collapses those into native_included_used_percent only and does not
    # retain monthlyLimit/used on the quota blob usage_report sees — cannot emit USD.
    return _quota_percent_row(used=used, observed_at=observed_at, resets_at=resets_at)


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
        elif provider == 'codex':
            out.extend(_codex_quota_percent_rows(preflight))
        elif provider == 'grok':
            grok = _grok_quota_percent_row(preflight)
            if grok is not None:
                out.append(grok)
    except Exception:
        return out[:8]
    return out[:8]
