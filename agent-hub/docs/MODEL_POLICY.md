# Strongest eligible, distinct models

`agent_hub.model_policy` selects models for Codex, Claude Code, Cursor, GitHub Copilot and Grok Build from explicit, fresh account evidence. It makes no provider calls, reads no credentials, and installs or changes nothing. `agent_hub.model_runtime` binds a selected worker command to its expected account and installed native binary. **Live catalog collectors, automatic refresh scheduling and a shared room plan are separate integrations; these modules do not claim they exist.**

The default `included_then_existing_credits` policy uses available included subscription usage first, then permits existing purchased credits when the provider enforces a verified remaining balance and spend cap. Automatic reload, purchases and API fallback must be disabled. The optional `subscription_only` policy requires disabled overages. API billing, unapproved overages, exhausted allowances, controller exclusions and inaccessible models are excluded. Unknown eligibility, cost enforcement, identity, rank or effective capabilities block selection. A public model list alone does not establish account access or billing safety. No model name, release date, price or self-reported confidence is used as a quality ranking.

Every selected underlying canonical model must be different. The algorithm considers all requested agents together, so an early greedy choice cannot strand another agent unnecessarily. Subject to uniqueness, it first minimizes the number of selections using existing credits, then minimizes the sorted worst-first list of ordinal strength losses from each agent's best eligible rank. Among equally strong assignments it prefers more distinct model families, then resolves ties deterministically by canonical ID. Thus included usage takes priority over a stronger credit-only model, and family diversity is a preference after strength; exact model uniqueness is never relaxed. Different model identities do not prove statistically independent judgments.

The strongest model for every agent independently can conflict with the requirement that no two agents share a model. `strength_loss_profile` and each selection's `strength_loss_levels` make that tradeoff explicit. `strength_rank` is a documented controller ranking, lower being stronger; equal ranks denote ties. The optimum is relative to those declared rankings, not a claim of a universal model benchmark. If the search budget is exhausted or no complete distinct assignment exists, no partial plan is returned.

## Current official integration evidence

These sources were checked on September 20, 2026. They document integration controls, not this user's entitlement. Prefer fresh account-effective data and installed-version capabilities over static examples.

| Agent | Documented catalog/discovery route | Explicit execution controls |
| --- | --- | --- |
| Codex | App-server `model/list`, including `supportedReasoningEfforts`, pagination and upgrade metadata | `--model ID --config model_reasoning_effort="VALUE"` |
| Claude Code | Account's `/model` picker and effective effort controls; a supported SDK collector may be added when its version/schema is verified | `--model ID --effort VALUE` |
| Cursor | CLI `--list-models`; SDK `Cursor.models.list()` includes account/team-specific parameters and variants | `--model VERIFIED_VARIANT_ID`; no general CLI effort flag was established |
| Copilot | `/model` or `/models`; SDK `listModels()` includes capabilities, billing and policy | `--model ID --effort VALUE` |
| Grok Build | `grok models`; installed `--help` and catalog capability evidence | `--model ID --effort VALUE` |

Codex documents [account model discovery](https://learn.chatgpt.com/docs/app-server#list-models-modellist), [reasoning configuration](https://learn.chatgpt.com/docs/config-file/config-reference) and [recommended models](https://learn.chatgpt.com/docs/models). The model page currently describes Astra as its most capable model, but that statement does not authorize a paid route. The static configuration reference and live catalogs can differ in effort options. Require the installed CLI and selected model to support the same effective setting; this policy does not hardcode a universal maximum.

Claude's [model configuration](https://code.claude.com/docs/en/model-config) and [CLI reference](https://code.claude.com/docs/en/cli-reference) explain model-specific effort, aliases, account restrictions and precedence. Managed or organization effort caps can silently clamp JSON runs. `best` can resolve to Fable; noninteractive Fable requests may bill usage credits without a consent prompt. Consequently, never treat `best`, `opus`, `default`, or a public capability table as verified subscription eligibility. Remove conflicting environment/settings overrides, avoid fallback chains, and establish effective account caps before dispatch. An effort unsupported by the installed CLI cannot be made valid by writing it into a catalog.

Cursor's [CLI parameters](https://cursor.com/docs/cli/reference/parameters) establish model listing and selection. Its [SDK model catalog](https://cursor.com/docs/sdk/typescript) provides model-specific parameters/variants and is account/team-specific. An API-key catalog is not proof of subscription CLI billing. A collector must show that a particular listed CLI model ID actually selects the desired effort variant; SDK parameters cannot be silently translated into an invented CLI flag. If this cannot be established, block that candidate.

Copilot's [command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) documents effort choices and model selection. Its [SDK compatibility reference](https://docs.github.com/en/copilot/how-tos/copilot-sdk/troubleshooting/compatibility) documents capability/billing/policy discovery. Automatic model routing and subagent fallback may change the underlying model. Disable such behavior or require an enforced model policy and verify the resolved runtime metadata. The full CLI effort list does not mean every model supports every value.

Grok's [CLI reference](https://docs.x.ai/build/cli/reference) documents model listing and model/effort flags. API-model discovery is not interchangeable with a Build subscription catalog. Resolve retired and rolling aliases before selection and establish account allowance plus enforced billing controls. This implementation does not infer Build entitlement from an xAI API key or from a Grok website subscription label.

## Trusted collector contract

The JSON bundle is controller-owned evidence, not task-supplied configuration. A checksum detects changes; it is not a signature or authorization grant. A protected collector must verify every assertion below against the intended account, installed binary, current provider settings and documented model identity. Store source receipts outside this bounded bundle, addressed by SHA-256. Do not place tokens, emails, browser state, prompts or raw account responses here.

```text
bundle = {
  schema_version: 1,
  registry: {
    observed_at: epoch_seconds, expires_at: epoch_seconds,
    revision: bounded_id, evidence_sha256: receipt_hash,
    models: {
      "provider:exact-underlying-model": {
        family: "provider-family", identity_evidence_sha256: receipt_hash
      }
    },
    aliases: {"product:alias-or-variant": "provider:exact-underlying-model"}
  },
  catalogs: {
    agent_id: {
      observed_at, expires_at, revision, evidence_sha256,
      complete: true, source: "account_catalog",
      account_ref: opaque_sha256, cli_version: bounded_id, cli_sha256: binary_hash,
      auth_route: "subscription", overage_disabled: true,
      ranking_evidence_sha256: current_ranking_receipt_hash,
      candidates: [{
        model_ref: registry_key_or_alias,
        accessible: true, billing: "subscription_included", allowance: "available",
        strength_rank: positive_integer,
        effort_control: control_name,
        effective_capabilities_verified: true,
        capability_evidence_sha256: receipt_hash,
        efforts: [{
          level: provider_specific_level, cli_model_id: exact_listed_cli_id,
          model_ref: registry_key_or_alias, pin_verified: true
        }]
      }]
    }
  }
}
```

`efforts` is an evidenced, least-to-most order for that exact model, account and CLI. The last entry is selected. Each variant must resolve to the candidate's same canonical model. Effort names have no cross-model numeric meaning. Use controls `codex-config`, `claude-effort`, `copilot-effort`, `grok-effort`, or Cursor's `model-variant`. `fixed` is allowed only when the provider proves there is no configurable effort; its sole level must be `not_configurable`, and the result must not be advertised as configurable maximum reasoning. Missing effort evidence is not equivalent to fixed effort.

The exact `[1m]` context suffix is supported for execution IDs only with an explicit registry alias resolving to the same canonical model as its base reference. For example, a verified Claude extended-context selector remains intact in argv but cannot count as a different underlying model. Arbitrary suffix stripping and unregistered variants are rejected.

When `overage_disabled` is false, the default policy requires catalog `credit_controls` containing `verified: true`, `provider_cap_enforced: true`, integer `existing_balance_microusd` and `remaining_spend_cap_microusd`, `auto_reload_enabled: false`, `automatic_purchase_enabled: false`, `api_fallback_enabled: false`, and receipt hashes `evidence_sha256` and `pool_ref`. Credit-eligible candidates use `billing: "existing_credits"`. Values must reflect current provider enforcement, not a suggested hub cap or an unverified dashboard estimate.

Selections report `credit_enforcement` (`provider_existing_balance_and_cap` or `provider_disabled`), `provider_credit_allowance_microusd` (the lesser of the verified remaining balance and provider cap), and `credit_pool_ref`. This is an observed provider allowance, not a per-call reservation, exact call price, or additional budget for every worker. References to the same pool describe shared funds and must not be added together. Provider enforcement is the admission guard; these modules neither create a hub ledger nor invent a new spending limit. A separately authorized hub budget may add its own guard. Unknown provider enforcement blocks credit use; a zero enforced cap still permits verified included usage.

Catalog freshness covers identity resolution, ranking, entitlement, allowance, effective effort caps and CLI binding—not merely the time a cached file was copied. Paginate fully. If a new account-eligible model appears but its identity, ranking or controls remain unresolved, fail closed and request collector refresh. Known inaccessible, metered or exhausted candidates can be excluded without pretending they are available. Never invent fresh timestamps or fill unknown fields with optimistic defaults.

The controller independently maps each worker to its intended account. `account_ref` should be the configured opaque identity reference, such as a hash of the normalized owner identifier; the policy does not read or store that identifier. Before launch, the worker must compare the selected reference against its separately trusted expected account, verify the authenticated session belongs to it, and compare the actual executable hash with `cli_sha256`. An account reference copied from the same untrusted bundle is not an independent ownership check.

## Runtime use and bounds

1. Fetch/normalize complete account catalogs without inference, resolve model identities across products, and validate the current ranking/effective-capability receipts.
2. Call `select_stack(bundle, agents=(...), now=integer_epoch, policy=SelectionPolicy(...))` for the whole collaborating stack. Defaults request all five agents; no default four-agent room behavior is changed by this module.
3. Persist the returned plan in trusted controller storage. Immediately before each launch, refresh if required and call `validate_plan(plan, bundle, ...)`. Compare the intended account and actual binary independently; verify quota and provider cost enforcement again. Apply a separate hub budget only if one was explicitly configured.
4. Append `cli_selection_args(plan['selections'][agent])` to the fixed, restricted command without using a shell. Reject conflicting model/effort flags, environment overrides, configuration sources, subagent routing and automatic fallback. Model selection never grants additional tools or permissions.
5. Verify authoritative CLI/provider model and effort metadata with `verify_observed_selection(...)`. Model-written text is not identity evidence. Unknown or mismatched metadata cannot count as compliant success; retain incurred costs and do not automatically rerun it.

Defaults bound catalogs to one hour of age, plans to five minutes, candidates to 24 per agent, selection search to 100,000 examined candidates and the complete bundle to 256,000 serialized bytes. Earlier provider expiry wins. Search exhaustion returns an error even after finding a feasible intermediate assignment. Alias cycles, conflicting duplicate capabilities and unresolved mappings also fail closed.

Changing account references, binary hashes, catalog revisions, aliases, rankings, exclusions or any other bundle content invalidates the old plan. A new verified stronger entry will be selected automatically on the next successful full selection. This is **selection on refreshed evidence**, not an implemented background update service. Until collectors and runtime gates are installed, the module alone does not enforce any running worker's model.

`select_worker_model(agent_id, bundle_path, expected_account_ref, command, *, now=None, execution_mode='read_only', active_agents=AGENTS)` reloads bounded strict JSON, selects the configured fleet, validates the independent account reference, hashes the native executable, and adds explicit selection arguments to the exact canonical adapter command. The default fleet contains all five agents. A trusted deployment may explicitly configure `active_agents=('claude',)` to onboard and smoke-test its first enrolled worker without fabricated catalogs for the others; uniqueness then applies only to that configured fleet. This limited profile must be labeled as such and is not five-agent readiness. Neither a task nor a failed full-stack selection may reduce the fleet automatically.

Codex arguments are inserted before its final `-`, preserving stdin and prompt bindings. Wrappers, scripts, duplicate JSON keys, linked paths and conflicting command flags are rejected. Both `execution_mode` and `active_agents` come only from trusted worker configuration; the exact adapter permission profile must match the supplied command and the worker must belong to the active fleet. The caller must protect the bundle and executable with immutable control-plane mounts and validate enrollment; path and digest checks alone do not establish ownership or authorization.

Independent refreshes by separate workers can choose different stack revisions. They do **not** guarantee globally distinct models throughout one room; a coordinator must pin one plan and bundle for the room. These modules do not supply that coordinator or live account collectors. Authoritative same-process applied metadata remains necessary to detect provider-side clamping or fallback.

The policy never switches to API billing, buys credits, reloads balances, increases limits or treats missing usage as free. Highest effort can consume included allowances and authorized existing credits faster. When neither verified included access nor enforced existing-credit access is available, selection pauses instead of silently lowering effort or changing billing routes. A provider may stop a call when its shared cap is reached; the policy does not claim a pre-call cost estimate or guaranteed completion.

## Verification

`python -W error::ResourceWarning -m unittest tests.test_model_policy tests.test_model_runtime -v` runs synthetic offline tests. They cover global non-greedy assignment, alias deduplication, distinct identities, strength/diversity ordering, future model updates, account/CLI binding changes, provider-specific flags, unknown billing/capabilities, included-first credit selection, provider-enforced caps without invented reservations, freshness, search bounds, actual-metadata mismatch and launch-command integrity. Synthetic future model names, effort values and native-header fixtures demonstrate data-driven selection; they are not live catalogs or executable provider clients.
