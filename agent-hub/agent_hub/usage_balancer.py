"""Pure, bounded allocation of queued work across verified subscription routes.

Inputs are trusted controller receipts, not assertions accepted from task text.
This planner neither authenticates receipts nor launches/retries work. Its
reservations are estimates within one plan, not enforced billing limits. The
controller must atomically claim work and revalidate all receipts before launch.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re


class BalanceError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise BalanceError(message)


def _id(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/+\[\]-]{0,159}", value), "Invalid bounded identifier")


def _number(value, maximum=1e11, minimum=0):
    _require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum, "Invalid finite measurement")


def _integer(value, maximum=1024):
    _require(type(value) is int and 0 <= value <= maximum, "Invalid bounded count")


def _typed(values, kind, maximum=1024):
    _require(type(values) is tuple and len(values) <= maximum and all(isinstance(v, kind) for v in values), "Expected bounded typed tuple")


def _ids(values, maximum=32):
    _typed(values, str, maximum)
    for value in values:
        _id(value)
    _require(len(set(values)) == len(values), "Duplicate identifier")


def _sha(value):
    _require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value), "Expected evidence SHA-256")


def _fresh(observed, now, age):
    return observed is not None and 0 <= now - observed <= age


@dataclass(frozen=True, order=True)
class Account:
    provider: str
    account_id: str

    def __post_init__(self):
        _id(self.provider); _id(self.account_id)


@dataclass(frozen=True)
class QuotaPool:
    id: str
    accounts: tuple[Account, ...]
    billing: str  # included | existing_credits | unknown | unlimited
    remaining_fraction: float | None
    observed_at: float | None
    resets_at: float | None
    evidence_sha256: str | None = None
    status: str = "available"
    credits_approved: bool = False
    provider_cap_enforced: bool = False
    auto_reload_disabled: bool = False
    automatic_purchase_disabled: bool = False
    api_fallback_disabled: bool = False
    inflight_fraction: float = 0
    # Controller's ledger for this exact pool/reset window, including failed,
    # uncertain and in-flight improvement usage. None is not assumed to be zero.
    improvement_committed_fraction: float | None = None

    def __post_init__(self):
        _id(self.id); _typed(self.accounts, Account, 64)
        _require(bool(self.accounts) and len(set(self.accounts)) == len(self.accounts), "Pool needs distinct authoritative account members")
        _require(self.billing in ("included", "existing_credits", "unknown", "unlimited"), "Unknown billing class")
        _require(self.status in ("available", "unavailable", "auth_failed"), "Unknown quota status")
        if self.remaining_fraction is not None:
            _number(self.remaining_fraction, 1)
        _number(self.inflight_fraction, 1)
        if self.improvement_committed_fraction is not None:
            _number(self.improvement_committed_fraction, 1)
        for value in (self.observed_at, self.resets_at):
            if value is not None:
                _number(value)
        if self.evidence_sha256 is not None:
            _sha(self.evidence_sha256)
        for flag in (self.credits_approved, self.provider_cap_enforced, self.auto_reload_disabled,
                     self.automatic_purchase_disabled, self.api_fallback_disabled):
            _require(type(flag) is bool, "Credit controls must be explicit booleans")


@dataclass(frozen=True)
class Worker:
    """Exact current model AND maximum supported effort verified by model policy."""
    id: str
    agent_id: str
    account: Account
    model_id: str
    effort: str
    capabilities: tuple[str, ...]
    observed_at: float
    authenticated: bool = False
    available: bool = False
    maximum_model_verified: bool = False
    model_observed_at: float | None = None
    active: int = 0
    concurrency: int = 1
    last_assigned_at: float = 0
    fleet_plan_sha256: str | None = None

    def __post_init__(self):
        for value in (self.id, self.agent_id, self.model_id, self.effort):
            _id(value)
        _require(isinstance(self.account, Account), "Worker needs account identity")
        _ids(self.capabilities); _number(self.observed_at); _number(self.last_assigned_at)
        if self.model_observed_at is not None:
            _number(self.model_observed_at)
        if self.fleet_plan_sha256 is not None:
            _sha(self.fleet_plan_sha256)
        for value in (self.authenticated, self.available, self.maximum_model_verified):
            _require(type(value) is bool, "Worker verification flags must be booleans")
        _integer(self.active); _integer(self.concurrency)


@dataclass(frozen=True)
class Concurrency:
    """Explicit provider and account limits, including work outside this batch."""
    provider: str
    account_id: str | None
    active: int
    limit: int
    observed_at: float

    def __post_init__(self):
        _id(self.provider)
        if self.account_id is not None:
            _id(self.account_id)
        _integer(self.active); _integer(self.limit); _number(self.observed_at)


@dataclass(frozen=True)
class Route:
    id: str
    worker_id: str
    pool_ids: tuple[str, ...]
    billing: str

    def __post_init__(self):
        _id(self.id); _id(self.worker_id); _ids(self.pool_ids)
        _require(bool(self.pool_ids), "A route must declare every charge/quota pool")
        _require(self.billing in ("included", "existing_credits"), "Unknown route billing")


@dataclass(frozen=True)
class Estimate:
    task_id: str
    route_id: str
    model_id: str
    effort: str
    demands: tuple[tuple[str, float], ...]
    observed_at: float

    def __post_init__(self):
        for value in (self.task_id, self.route_id, self.model_id, self.effort):
            _id(value)
        _typed(self.demands, tuple, 32); _number(self.observed_at)
        for demand in self.demands:
            _require(len(demand) == 2, "Demand must name pool and normalized fraction")
            _id(demand[0]); _number(demand[1], 1, 1e-12)
        _require(len({v[0] for v in self.demands}) == len(self.demands), "Duplicate estimated pool")


@dataclass(frozen=True)
class Task:
    id: str
    queued_at: float
    capabilities: tuple[str, ...]
    origin_surface: str = "chatgpt"
    origin_account: str = "unknown"
    allowed_workers: tuple[str, ...] = ()
    prior_attempt: str = "never_started"
    purpose: str = 'project'

    def __post_init__(self):
        _id(self.id); _number(self.queued_at); _ids(self.capabilities)
        _id(self.origin_surface); _id(self.origin_account); _ids(self.allowed_workers, 128)
        _require(self.prior_attempt in ("never_started", "reconciled", "uncertain"), "Unknown prior attempt state")
        _require(self.purpose in ('project', 'improvement'), 'Unknown task purpose')


@dataclass(frozen=True)
class OrchestrationReserve:
    """Verified ChatGPT sharing relationship, never inferred from an email."""
    origin_account: str
    pool_id: str
    fraction: float
    observed_at: float
    shared_pool_evidence_sha256: str

    def __post_init__(self):
        _id(self.origin_account); _id(self.pool_id); _number(self.fraction, 1)
        _number(self.observed_at); _sha(self.shared_pool_evidence_sha256)


@dataclass(frozen=True)
class Policy:
    max_usage_age: float = 900
    max_worker_age: float = 90
    max_model_age: float = 900
    max_estimate_age: float = 86400
    estimate_multiplier: float = 1.25
    minimum_reset_horizon: float = 60
    credit_planning_horizon: float = 86400
    worker_starvation_seconds: float = 3600
    max_assignments: int = 100
    improvement_fraction_cap: float = 0.25
    improvement_minimum_remaining: float = 0.25
    foreground_active: bool = False

    def __post_init__(self):
        for value in (self.max_usage_age, self.max_worker_age, self.max_model_age,
                      self.max_estimate_age, self.minimum_reset_horizon,
                      self.credit_planning_horizon, self.worker_starvation_seconds):
            _number(value, 31_536_000, 1)
        _number(self.estimate_multiplier, 10, 1); _integer(self.max_assignments)
        _number(self.improvement_fraction_cap, 0.25)
        _number(self.improvement_minimum_remaining, 1, 0.25)
        _require(type(self.foreground_active) is bool, 'Foreground activity must be explicit')


@dataclass(frozen=True)
class Decision:
    task_id: str
    action: str
    reason: str
    origin_surface: str
    origin_account: str
    worker_id: str | None = None
    route_id: str | None = None
    account: Account | None = None
    model_id: str | None = None
    effort: str | None = None
    billing: str | None = None
    score: float | None = None
    estimated_reservations: tuple[tuple[str, float], ...] = ()
    rejected_routes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Plan:
    decisions: tuple[Decision, ...]
    estimated_pool_remaining: tuple[tuple[str, float | None], ...]
    orchestration_reserved: tuple[tuple[str, float], ...]
    notices: tuple[str, ...]
    observed_at: float


def plan_dispatch(tasks: tuple[Task, ...], workers: tuple[Worker, ...],
                  routes: tuple[Route, ...], pools: tuple[QuotaPool, ...],
                  estimates: tuple[Estimate, ...], concurrency: tuple[Concurrency, ...],
                  *, now: float, fleet_plan_sha256: str, policy: Policy = Policy(),
                  orchestration_reserves: tuple[OrchestrationReserve, ...] = ()) -> Plan:
    """Return an advisory batch; never execute, reset quotas, buy, or retry.

    FIFO prevents younger compatible tasks overtaking older eligible work. Each
    route's score is its limiting pool's estimated similar-task headroom per
    hour until reset. Credits without a reset use an explicit planning horizon,
    not an invented refill. Long-idle eligible workers receive a fairness turn.
    """
    _number(now); _sha(fleet_plan_sha256); _require(isinstance(policy, Policy), "Expected policy")
    for values, kind, bound in ((tasks, Task, 256), (workers, Worker, 128), (routes, Route, 256),
                                (pools, QuotaPool, 256), (estimates, Estimate, 4096),
                                (concurrency, Concurrency, 256), (orchestration_reserves, OrchestrationReserve, 128)):
        _typed(values, kind, bound)
    def indexed(values, key):
        result = {}
        for value in values:
            name = key(value)
            _require(name not in result or result[name] == value, "Conflicting duplicate record")
            result[name] = value
        return result
    ts = indexed(tasks, lambda v: v.id)
    ws = indexed(workers, lambda v: v.id)
    rs = indexed(routes, lambda v: v.id)
    ps = indexed(pools, lambda v: v.id)
    es = indexed(estimates, lambda v: (v.task_id, v.route_id))
    cs = indexed(concurrency, lambda v: (v.provider, v.account_id))
    for route in rs.values():
        _require(route.worker_id in ws and all(p in ps for p in route.pool_ids), "Unknown route worker/pool")
        _require(all(ws[route.worker_id].account in ps[p].accounts for p in route.pool_ids), "Pool account binding mismatch")
        _require(all(ps[p].billing == route.billing or ps[p].billing in ("unknown", "unlimited") for p in route.pool_ids), "Route billing mismatch")
    for estimate in es.values():
        _require(estimate.task_id in ts and estimate.route_id in rs, "Unknown estimate task/route")
    for key, limit in cs.items():
        known_workers = sum(w.active for w in ws.values() if w.account.provider == key[0]
                            and (key[1] is None or w.account.account_id == key[1]))
        _require(limit.active >= known_workers, "Concurrency receipt undercounts known active workers")
        if key[1] is None:
            known_accounts = sum(c.active for k, c in cs.items() if k[0] == key[0] and k[1] is not None)
            _require(limit.active >= known_accounts, "Provider concurrency undercounts account activity")
    remaining = {p.id: None if p.remaining_fraction is None else max(0, p.remaining_fraction - p.inflight_fraction)
                  for p in ps.values()}
    improvement_committed = {p.id: p.improvement_committed_fraction for p in ps.values()}
    foreground_pending = policy.foreground_active or any(t.purpose == 'project' for t in ts.values())
    reserved, blocked_pools = {}, {}
    bindings = indexed(orchestration_reserves, lambda v: (v.origin_account, v.pool_id))
    for reserve in bindings.values():
        _require(reserve.pool_id in ps, "Unknown orchestration pool")
        if not _fresh(reserve.observed_at, now, policy.max_usage_age):
            blocked_pools[reserve.pool_id] = "orchestration_binding_stale"
        reserved[reserve.pool_id] = reserved.get(reserve.pool_id, 0) + reserve.fraction
        _require(reserved[reserve.pool_id] <= 1, "Orchestration reservations exceed pool capacity")
    worker_active = {w.id: w.active for w in ws.values()}
    scope_active = {key: c.active for key, c in cs.items()}
    last_assigned = {w.id: w.last_assigned_at for w in ws.values()}
    notices = {"advisory_estimates_require_atomic_claim_and_fresh_launch_validation"}
    for task in ts.values():
        if task.origin_surface == "chatgpt":
            notices.add("chatgpt_conversation_usage_unavailable:" + task.origin_account)
    decisions = []
    assignments = 0

    def assess(task, route):
        w = ws[route.worker_id]
        if task.purpose == 'improvement' and route.billing != 'included':
            return 'improvement_never_uses_purchased_credits', None
        if task.allowed_workers and w.id not in task.allowed_workers:
            return "worker_not_allowed", None
        if not set(task.capabilities) <= set(w.capabilities):
            return "task_unsuitable", None
        if not _fresh(w.observed_at, now, policy.max_worker_age):
            return "worker_observation_stale", None
        if not w.authenticated:
            return "authentication_unverified", None
        if not w.available:
            return "worker_unavailable", None
        if w.fleet_plan_sha256 != fleet_plan_sha256:
            return "fleet_model_plan_mismatch", None
        if not w.maximum_model_verified or not _fresh(w.model_observed_at, now, policy.max_model_age):
            return "maximum_model_unverified_or_stale", None
        est = es.get((task.id, route.id))
        if not est or not _fresh(est.observed_at, now, policy.max_estimate_age):
            return "consumption_estimate_missing_or_stale", None
        if (est.model_id, est.effort) != (w.model_id, w.effort):
            return "estimate_model_effort_mismatch", None
        if set(p for p, _ in est.demands) != set(route.pool_ids):
            return "incomplete_pool_estimates", None
        demands, scores = [], []
        for pid, fraction in est.demands:
            p = ps[pid]
            if pid in blocked_pools:
                return blocked_pools[pid], None
            if p.billing in ("unknown", "unlimited"):
                return "unbounded_or_unknown_billing_pool", None
            if p.status != "available" or p.remaining_fraction is None or p.evidence_sha256 is None:
                return "quota_unavailable_or_unverified", None
            if not _fresh(p.observed_at, now, policy.max_usage_age):
                return "quota_observation_stale", None
            if p.resets_at is not None and p.resets_at <= now:
                return "quota_reset_requires_refresh", None
            if p.billing == "included" and p.resets_at is None:
                return "quota_reset_unknown", None
            if p.billing == "existing_credits" and not all((p.credits_approved, p.provider_cap_enforced,
                    p.auto_reload_disabled, p.automatic_purchase_disabled, p.api_fallback_disabled)):
                return "existing_credit_controls_unverified", None
            demand = fraction * policy.estimate_multiplier
            usable = remaining[pid] - reserved.get(pid, 0)
            if task.purpose == 'improvement':
                committed = improvement_committed[pid]
                if committed is None:
                    return 'improvement_ledger_unknown', None
                if committed + demand > policy.improvement_fraction_cap + 1e-12:
                    return 'improvement_budget_exhausted', None
                if usable - demand < policy.improvement_minimum_remaining - 1e-12:
                    return 'foreground_allowance_reserve', None
            if usable + 1e-12 < demand:
                return "insufficient_pool_headroom", None
            horizon = p.resets_at - now if p.resets_at is not None else policy.credit_planning_horizon
            scores.append(usable / demand / (max(horizon, policy.minimum_reset_horizon) / 3600))
            demands.append((pid, demand))
        for key in ((w.account.provider, None), (w.account.provider, w.account.account_id)):
            limit = cs.get(key)
            if not limit or not _fresh(limit.observed_at, now, policy.max_worker_age):
                return "concurrency_limit_missing_or_stale", None
        result = (min(scores), tuple(sorted(demands)))
        if (worker_active[w.id] >= w.concurrency or any(scope_active[k] >= cs[k].limit for k in
                ((w.account.provider, None), (w.account.provider, w.account.account_id)))):
            return "concurrency_full", result
        return None, result

    for task in sorted(ts.values(), key=lambda t: (t.queued_at, t.id)):
        base = dict(task_id=task.id, origin_surface=task.origin_surface, origin_account=task.origin_account)
        if task.prior_attempt == "uncertain":
            decisions.append(Decision(**base, action="blocked", reason="uncertain_charge_requires_reconciliation"))
            continue
        if task.queued_at > now:
            decisions.append(Decision(**base, action="blocked", reason="task_timestamp_in_future"))
            continue
        if task.purpose == 'improvement' and foreground_pending:
            decisions.append(Decision(**base, action='wait', reason='foreground_work_has_priority'))
            continue
        if assignments >= policy.max_assignments:
            decisions.append(Decision(**base, action="wait", reason="batch_limit"))
            continue
        eligible, rejected, included_busy, included_unresolved = [], [], False, False
        unresolved = {"consumption_estimate_missing_or_stale", "estimate_model_effort_mismatch",
                      "incomplete_pool_estimates", "orchestration_binding_stale",
                      "unbounded_or_unknown_billing_pool", "quota_unavailable_or_unverified",
                      "quota_observation_stale", "quota_reset_requires_refresh", "quota_reset_unknown",
                      "concurrency_limit_missing_or_stale"}
        for route in sorted(rs.values(), key=lambda r: r.id):
            reason, result = assess(task, route)
            if reason:
                rejected.append((route.id, reason))
                included_busy |= reason == "concurrency_full" and route.billing == "included"
                included_unresolved |= reason in unresolved and route.billing == "included"
            else:
                eligible.append((route, result))
        included = [item for item in eligible if item[0].billing == "included"]
        if included:
            eligible = included
        elif included_unresolved:
            decisions.append(Decision(**base, action="wait", reason="included_route_measurement_unresolved", rejected_routes=tuple(rejected)))
            continue
        elif included_busy:
            decisions.append(Decision(**base, action="wait", reason="included_route_busy", rejected_routes=tuple(rejected)))
            continue
        if not eligible:
            decisions.append(Decision(**base, action="wait", reason="no_eligible_route", rejected_routes=tuple(rejected)))
            continue
        aged = [item for item in eligible if now - last_assigned[item[0].worker_id] >= policy.worker_starvation_seconds]
        if aged:
            chosen = min(aged, key=lambda item: (last_assigned[item[0].worker_id], -item[1][0], item[0].id))
            reason = "oldest_eligible_worker_fairness_turn"
        else:
            chosen = min(eligible, key=lambda item: (-item[1][0], last_assigned[item[0].worker_id], item[0].id))
            reason = "greatest_reset_adjusted_headroom"
        route, (score, demands) = chosen
        w = ws[route.worker_id]
        for pid, demand in demands:
            remaining[pid] = max(0, remaining[pid] - demand)
            if task.purpose == 'improvement':
                improvement_committed[pid] += demand
        worker_active[w.id] += 1
        for key in ((w.account.provider, None), (w.account.provider, w.account.account_id)):
            scope_active[key] += 1
        last_assigned[w.id] = now
        assignments += 1
        decisions.append(Decision(**base, action="assign", reason=reason, worker_id=w.id,
            route_id=route.id, account=w.account, model_id=w.model_id, effort=w.effort,
            billing=route.billing, score=score, estimated_reservations=demands, rejected_routes=tuple(rejected)))
    return Plan(tuple(decisions), tuple(sorted(remaining.items())), tuple(sorted(reserved.items())), tuple(sorted(notices)), now)
