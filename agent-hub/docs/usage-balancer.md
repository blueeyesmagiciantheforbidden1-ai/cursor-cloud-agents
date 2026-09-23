# Usage-aware work allocation

`agent_hub.usage_balancer.plan_dispatch()` is an offline batch planner. It does
not start workers, spend credits, alter subscription settings, or retry calls.
The cloud dispatcher must integrate it before it changes live task allocation.

## Trusted input contract

Pass bounded tuples of `Task`, `Worker`, `Route`, `QuotaPool`, `Estimate`, and
`Concurrency` receipts with an explicit current Unix timestamp. Account identity
is `(provider, account_id)`, so two Codex accounts remain separate. Account IDs
may be opaque hashes. Task origin labels identify either ChatGPT account without
implying that the ChatGPT account owns the selected worker subscription.

The controller supplies canonical pool IDs from authoritative provider account
and window metadata. Aliases for one real quota must reference the same ID.
Identical duplicate pool receipts are deduplicated; conflicting duplicates are
rejected. Every route declares all pools it can consume, including simultaneous
short and weekly windows. Every estimate names those exact pools and the exact
selected model and effort. Estimates are fractions of each finite pool's total
allowance, not dollar or token values compared across unrelated products.
Missing estimates and unknown or unlimited billing pools are ineligible.

`maximum_model_verified` attests that the existing model policy selected the
current strongest supported model and effective maximum effort. The planner
does not select cheaper models or change effort. Model policy, account identity,
capability evaluation, and receipt authentication remain controller duties;
these dataclasses are not cryptographic proof of the caller's assertions.
Every call requires `fleet_plan_sha256`, and every eligible worker must carry
that same reference. The controller must produce one shared immutable fleet
plan that already verifies model diversity; independent per-worker maximum
model assertions or separate plans do not establish cross-agent uniqueness.

For credits, normalize against a known finite provider allowance bounded by the
existing credit balance and provider spending cap. Require user approval,
provider cap enforcement, and disabled auto-reload, automatic purchases and API
fallback. This is provider billing enforcement, not an invented hub per-call
spend ceiling. A credit pool without a reset uses `credit_planning_horizon` for
ranking only; it never acquires an assumed refill.

Provide fresh worker, account and provider concurrency counts. Account and
provider counts must include activity outside this batch, and cannot undercount
known worker/account activity. `inflight_fraction` reserves estimated consumption
not yet reflected in a provider observation; do not count already-reported
consumption a second time. Existing unresolved charge outcomes must be reconciled
before a task can change from `prior_attempt='uncertain'` to `reconciled`.

## Allocation behavior

Tasks are considered oldest first. Unsuitable older tasks do not stop unrelated
eligible work. Eligible included routes take precedence over approved credit
routes. If otherwise eligible included capacity is merely busy, the task waits
instead of spending credits to bypass the concurrency limit.
Likewise, missing/stale included quota measurements or consumption estimates
require a refresh before choosing credits. Another confirmed included route may
still run. Known exhausted, unauthenticated or unsuitable routes do not create
this measurement wait, so unrelated workers cannot indefinitely block work.

For each route, the score is the minimum across its pools of:

```
usable remaining fraction / padded estimated task fraction / hours until reset
```

The greatest score receives work, subject to a configurable fairness turn for
a long-idle eligible worker. Default estimate padding is 25%. Each assignment
immediately reserves its estimates and concurrency in this batch. This steers
work toward unused allowance while reducing starvation; it cannot guarantee
equal depletion when provider reset windows, task suitability, model costs, or
measurement quality differ. It never generates extra work just to burn credits.

Freshness checks reject stale/future observations. A passed reset requires a new
provider receipt; the planner never automatically refills a pool. Decisions
retain origin, provider/account, unchanged model/effort, billing route, score,
estimated reservations, and per-route rejection reasons.

## ChatGPT orchestration allowance

ChatGPT conversation usage is explicitly reported unavailable by this module.
It is not equated with Codex usage even when emails or organizations match.
An `OrchestrationReserve` may reserve part of a worker quota only when an actual
shared-pool relationship has separate fresh authoritative evidence. Reserve
fractions for distinct origins are added; repeated identical bindings are
deduplicated. A stale shared-pool binding blocks that pool pending refresh.

## Required live integration

Display telemetry in `telemetry.py` currently lacks account and canonical pool
identities and must not be promoted directly into scheduling facts. Connect
provider account collectors and model policy first. Atomically claim queued work
and reserve known in-flight estimates, then revalidate authentication, model,
billing and concurrency immediately before launch. Persist the selected route
and reconciliation state. Repeatedly applying an old plan must not launch the
same task twice. Keep provider observations and controller reservations separate
so completed or expired work cannot manufacture new allowance.

Only deterministic offline tests exercise this module. No provider call is
required to run `python -m unittest tests.test_usage_balancer`.
# Background improvement allocation (2026-09-21)

The user selected 25% improvement / 75% project work. `Task.purpose` distinguishes controller-authorized improvement from project work. The planner waits on improvement while any foreground task is queued or active, disallows credit routes for improvement, caps its total committed plus estimated new usage at 0.25 per verified pool/reset window, and retains at least 0.25 remaining headroom for urgent work. Every route still requires current account/model/effort and quota evidence.

`QuotaPool.improvement_committed_fraction` must come from a durable controller ledger covering completed, failed, uncertain and in-flight work in that exact allowance window. Missing accounting stops improvement; it never initializes an unknown pool at zero. Reservations accumulate across one plan. Concurrent plans require a transaction and fresh revalidation; this pure function cannot enforce a provider-side cap or preempt an already charged model call. A receipt for an expired window must be refreshed rather than locally resetting the budget. Production automatic improvement dispatch remains off until that integration is verified.
