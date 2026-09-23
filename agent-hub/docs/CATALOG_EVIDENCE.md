# Evidence for the strongest eligible model without a complete picker

`agent_hub.catalog_evidence` is a pure, standalone receipt validator. It makes no provider calls, reads no files or credentials, and sends no prompts. It is **not wired into `model_policy`, the worker, or cloud configuration**. The existing complete-account-catalog path remains unchanged.

The current `model_policy._eligible` requires `source == "account_catalog"` and `complete == true`. That is a useful guard for an exhaustive ranked search, but completeness of all weaker models is unnecessary when a candidate demonstrably reaches the strongest possible rank. The new path keeps `coverage: "provider_upper_bound"` and `complete_account_catalog: false`. It never rewrites an incomplete picker as complete.

## The narrow claim that is sound

A reviewed current provider claim can establish that no model in the agent's applicable universe is stronger than a particular candidate under a declared **general coding capability** ranking. If that candidate is actually eligible for the intended account, choosing it reaches the ranking upper bound without listing weaker models. This is a scoped provider-guidance claim, not a universal benchmark or proof that the model is best for every task. A newer release date, higher price, a model's self-description, or a locally accepted model string is insufficient.

`certify_frontier_stack` certifies a supplied stack only when every candidate reaches its provider upper bound and its best permitted billing class, all underlying canonical identities differ, and every agent has a different verified model family. Such a stack reaches the absolute bounds: minimum paid selections, zero strength loss, and one family per agent. Unlisted tied or weaker models cannot improve those three objectives. This establishes objective optimality, not the same lexical tie-break choice that a complete catalog might produce.

If identities conflict or fewer than one family per agent are represented, the helper requests more evidence. The current choice might still be optimal, but this small proof does not establish that. It never silently lowers effort, chooses a weaker model, drops an agent, or invents unseen fallback candidates. A complete catalog or an expanded ranked frontier can resolve these cases later. Different model names or families do not prove statistical independence.

## Public guidance and the actual Claude receipt

The sanitized `fable-5-1-max-evidence.json` records native Claude 2.1.275 accepting the exact Fable 5.1 selector, advertising effort levels through `max`, and reporting that model/effort as applied. It records zero user messages and no inference. Its account response lacks subscription type; account ownership must remain bound by the separate enrollment evidence. This demonstrates local configuration/capability support, **not server entitlement or billing eligibility**. It is Windows evidence; it cannot stand in for the Linux binary/config binding.

Current official Claude documentation places Fable at the top of its general capability guidance and describes 5.1's improvements for long-running coding. This supports a reviewed, scoped upper-bound ranking without requiring the picker to enumerate every weaker option. It does not establish account access. [Fable 5.1 overview](https://platform.claude.com/docs/en/models/fable-5-1/overview)

Claude documents `max` support, possible account/managed effort clamps, and credit billing that can occur without a prompt in noninteractive runs. Therefore native applied settings and fresh model-specific billing evidence remain necessary. [Claude Code model configuration](https://code.claude.com/docs/en/model-config)

No live receipt is manufactured by this module. The current sanitized receipt still needs:

1. Current provider billing controls for this exact model/account: whether the route consumes included usage or existing purchased credits, and the relevant enforced limits. Confirm this **before** any eligibility prompt; do not learn the billing route by risking unbounded spend.
2. One separately authorized, bounded eligibility prompt on the intended deployment profile, with no project data/tools required. Retain authoritative provider/native result metadata for the actually served model, fallback status and billing route, together with the same-process applied effort and account binding. A self-reported model name is not evidence. Failure or uncertainty is recorded; this validator does not retry.
3. Fresh native capability/config receipts bound to the actual deployment executable and account. A successful Windows receipt does not certify a different Linux build.

An already completed, sufficiently fresh, properly bound real request can supply item 2; an extra paid call is not intrinsically required. A future documented provider entitlement endpoint could support a separately reviewed receipt kind without inference. This first version accepts completed-inference receipts only, so it does not accidentally elevate a picker/config response into entitlement proof.

## Minimal protected receipt contract

All references are opaque hashes or model identifiers. Do not include credentials, emails, prompts, raw transcripts or private paths. A SHA256 binds an immutable evidence artifact; it is not a signature or proof that a collector's assertion is true. Receipt creation and review must be protected from task content. Source URLs are references, not executable instructions or a domain-authentication mechanism.

Common proof metadata:

```text
observed_at: integer epoch seconds when the source was actually observed
expires_at: integer epoch seconds, preserving any earlier source expiry
evidence_sha256: digest of the immutable source receipt
```

Top-level receipt:

```text
schema_version: 1
coverage: "provider_upper_bound"
complete_account_catalog: false
agent: "codex" | "claude" | "cursor" | "copilot" | "grok"
account_ref: independently enrolled owner/account binding hash
cli_sha256: actual deployment native binary hash
cli_version: exact native version
canonical_id: verified provider:underlying-model identity
family: verified model family
cli_model_id: exact execution selector
identity_evidence_sha256: receipt proving selector/variant/canonical identity mapping
ranking: { ... }
native: { ... } | null
entitlement: { ... } | null
billing: { ... } | null
```

`ranking` has common proof metadata plus:

```text
kind: "official_provider_upper_bound"
agent, canonical_id
ranking_scope: "general_coding_capability"
universe: "all_models_available_through_agent"
no_stronger_model: true
source_urls: reviewed primary sources supporting that precise scoped assertion
```

The universe declaration must be justified. A Claude-only ranking cannot establish the strongest option across Cursor or Copilot's multiple providers. Special access programs, account-specific models, unknown provider options, or incompatible task/ranking scopes require further evidence. Do not infer that a public page enumerates private account entitlements.

Every `native`, `entitlement` and `billing` proof repeats `agent`, `account_ref`, `cli_sha256`, `canonical_id` and `cli_model_id`. Mismatched proofs cannot be combined. Each carries its own common metadata; copying a stale proof into a newly dated wrapper does not refresh it.

`native` additionally contains:

```text
kind: "native_applied_settings"
auth_route: "first_party_subscription"
applied_model: exact CLI selector
session_ref: opaque hash binding the native process/session
effective_config_sha256: reviewed effective configuration digest
account_effective_order_verified: true
effort_order: evidenced least-to-most supported levels for this model/account
applied_effort: last entry in effort_order
```

The module does not invent a universal `max` effort or infer an order from level names. Native settings acceptance is kept distinct from entitlement. Context variants such as `[1m]` must have explicit identity evidence and remain the same underlying model where that evidence says so; the module does not strip arbitrary suffixes.

`entitlement` additionally contains:

```text
kind: "provider_completed_inference"
accepted: true
served_canonical_id: exact candidate identity
model_identity_source: "provider_runtime_metadata"
fallback_occurred: false
request_binding_sha256: opaque binding to the completed request receipt
session_ref: same session as the native applied settings
effort: same applied effort
billing_route: "subscription_included" | "existing_credits"
```

Effort is bound to the same-process applied setting; this is not a claim that every provider exposes measured internal reasoning effort. The owning runtime must check its effective configuration again before each actual task and inspect authoritative result metadata for clamping/fallback where available.

`billing` additionally contains:

```text
kind: "provider_current_billing_controls"
provider_route_enforced: true
api_fallback_enabled: false
auto_reload_enabled: false
automatic_purchase_enabled: false
route: "subscription_included" | "existing_credits"
```

For included usage, require `included_route_allowed: true` and `paid_fallback: "disabled"`, or `paid_fallback: "verified_existing_credits"` with verified credit controls. For existing credits, require:

```text
credit_controls: {
  currency: "USD", provider_cap_enforced: true,
  existing_balance_microusd: positive verified integer,
  remaining_spend_cap_microusd: positive verified integer,
  pool_ref: shared credit-pool hash, evidence_sha256: receipt hash
}
```

These are existing provider-enforced amounts, not invented hub budgets or guaranteed per-call costs. Shared funds must not be added once per worker. The validator does not purchase/reload credits, alter limits, or invoke an API fallback. Its current currency schema requires verified USD units; unknown-unit balances are insufficient.

Under included-first policy a credit-funded top model can be eligible but not optimal: an unlisted weaker included model may be preferred. To certify a credit-funded frontier, the current billing receipt must additionally establish `all_included_routes_exhausted: true`, `included_exhaustion_scope: "all_models_available_through_agent"`, and `included_exhaustion_evidence_sha256`. Model-specific credit requirements are not account-wide exhaustion. Otherwise the assessment returns `included_first_frontier_not_proven`; collect a ranked included frontier or retain the complete-catalog path.

## Integration without weakening the existing policy

```python
assessment = assess_frontier(
    receipt, expected_agent="claude", expected_account_ref=trusted_owner_ref,
    expected_cli_sha256=actual_native_digest, now=trusted_time,
)
certificate = certify_frontier_stack(
    receipts_by_agent, expected_bindings=independent_deployment_bindings,
    now=trusted_time,
)
```

`assess_frontier` returns readiness and explicit missing evidence. `selection_eligible` means the candidate's evidence is sufficient; `cost_optimality_proven` is separate. `certify_frontier_stack` requires both, validates independent bindings again, and checks identity/family bounds. Both retain `complete_account_catalog: false` and `runtime_dispatch_authorized: false`. They are not accepted by the existing shared planner automatically.

A later root integration can add an explicit coverage branch: preserve existing complete-catalog validation unchanged, validate frontier receipts with this module, retain all existing identity/alias/rank/effort/account/billing checks, and store the frontier certificate separately in the immutable room plan. Do not pass it through the old path by setting `complete: true`. Mixed catalog/frontier optimization needs a reviewed objective proof; do not drop omitted candidates or merely run the old ordinal-loss calculation on a partial list.

Default evidence age is at most one hour; billing evidence is at most five minutes, and earlier source expiry wins. New models, updated provider guidance, account changes, binary/config updates, changed billing controls or elapsed freshness invalidate the corresponding receipt. This module is selection on evidence, **not a background refresh service**. Room-wide model-plan pinning and native preflight still belong to the controller/worker integration.

## Codex evidence

The existing two Codex enrollments produced genuine account-bound, fully paginated `model/list` responses and separate provider/quota-pool references. Those receipts should use the existing complete-account-catalog path when refreshed; there is no reason to replace richer evidence with a frontier assertion. Their saved timestamps must not be presented as current indefinitely. `ordinaryUsageAllowed` is an explicit account-bound permission; unknown or false must not be changed by inference from percentages or reset times.

The stored credit balance string has no verified currency or enforced-cap semantics. A displayed `250.0000000000` must not become USD, microUSD, spend authorization or a reset credit. Billing verification is independent from catalog completeness. This task did not refresh either account, read credentials or make model requests.

## Tests

`python -m unittest tests.test_catalog_evidence -v` passes 18 offline synthetic tests covering incomplete native evidence, stale proofs, wrong bindings, maximum effort, served-model/fallback proof, provider-ranking scope, unknown credit units, included-first priorities, exact identity conflicts and objective-bound stack certificates. The tests are contract verification, not provider entitlement or live model evidence.
