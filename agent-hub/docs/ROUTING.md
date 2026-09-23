# Offline routing and capability policy

`agent_hub.routing` makes deterministic admission decisions from explicit records. It does not discover accounts, call providers, provision infrastructure, download models, start inference, run code, train adapters, or spend money. Nothing in this module registers a real model as capable. Its test receipts are synthetic fixtures.

The default `RoutingPolicy()` refuses inference, training, and adapter promotion. An authorized controller can enable inference while leaving training and promotion disabled. Self-hosted inference always means an explicitly named **Google Cloud** deployment. Personal-PC runtimes and measurements are rejected, including when a caller uses the compatibility spelling `local-only` or `local_only`.

## Modes

| Mode | Decision |
| --- | --- |
| `frontier` | Select the strongest approved hosted model by the controller's declared `quality_rank`, at its highest declared supported reasoning effort. Require a fresh passing attestation for that exact configuration and capability, and enough approved budget. |
| `hybrid` | Try the strongest approved self-hosted Google Cloud model at its highest supported effort. Frontier escalation requires the caller to explicitly give `capability_gap`, `context_limit`, or `resources_unavailable`, matching an observed admission failure. |
| `self_hosted_only` | Apply the same cloud admission rules and abstain if unmet. Never select a hosted frontier provider. |

Equal model ranks use deterministic provider/model ordering; for cloud candidates, effort and estimated cost also break ties. Rank is an operator approval, not a model comparison performed by this module. Prefer registering only the approved deployment needed for a task. Effort names are the ordered policy vocabulary `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`; the controller must supply the actual supported subset. An effort value is never guessed from the model name.

There is no automatic downgrade to a weaker model or lower effort when the chosen model lacks evidence, exceeds the budget, or cannot be admitted. A missing highest-effort configuration abstains. An optional `requested_model=ModelSelection(provider, model, effort)` is an additional exact pin; it cannot override mode policy or silently resolve to another selection. A pin conflicting with the strongest approved configuration therefore abstains. Change approved configuration/rank explicitly when a different configuration is intended.

Successful cloud admission in hybrid mode returns a decision without escalation, even if an unused escalation reason was supplied. A budget failure cannot be labeled a resource failure to trigger a more expensive hosted call. When no approved cloud model is registered, hybrid can report a capability gap but still needs explicit escalation and separate valid frontier evidence.

## Capability and verification records

`Capability` binds an operation ID and immutable source/artifact SHA-256 to typed scalar inputs, explicit preconditions, allowed effects, required postconditions, and ordered fallback IDs. Its `digest` includes the complete contract. Conditions operate only on caller-supplied `input.*` or `fact.*` values; they cannot evaluate expressions, run commands, or inspect the machine. Effects are exact target aliases for reads, writes, network access, or model calls. The trusted executor owns the mapping from aliases to real resources and credentials.

`CapabilityRegistry` accepts at most 128 capabilities. It rejects unknown fallback targets, cycles, chains beyond the configured maximum (default four hops; maximum eight), expanded effects, or changed input/postcondition contracts. Resolution uses bounded breadth-first traversal and returns the chosen capability and full fallback chain. A fallback can satisfy alternative preconditions while preserving the requested output contract. Its own content digest needs its own model attestation.

`CapabilityAttestation` identifies the exact provider/model/effort, actual deployed model artifact ID, capability digest, verification evidence SHA-256, verifier identity, pass/fail result, observation time, evaluated context bound, evaluated concurrency, and measured peak RAM. Freshness defaults to one day and cannot exceed 30 days. A later failing receipt revokes an earlier pass; conflicting receipts at the latest identical timestamp fail closed. Duplicate identical receipts are harmless. Changed source, model artifact, effort, or capability contract requires new evidence. Future-dated or expired receipts cannot establish a passing capability.

These objects are **trusted-controller inputs**, not signed certificates. A hash identifies evidence; it does not prove the evidence exists or is true. The controller must authenticate the verifier, retrieve and verify evidence contents, and confirm the deployed model identity. Models must not supply their own approval, rank, attestation, permissions, prices, or resource observations. The module does not establish general model intelligence or claim a small model equals a frontier model.

The executor must independently check `capability.verify_postconditions(inputs, observed_facts)` after execution. A routing decision alone cannot claim success.

## Cloud resource and spending admission

`CloudResources` must name the configuration's Google Cloud deployment and contain a fresh measurement digest. Default measurement freshness is 60 seconds, bounded to at most five minutes. Admission requires:

- Exactly one already-loaded model, with the exact attested actual model ID. No implicit loading or replacement occurs.
- A nonzero attested peak RAM measurement and enough measured RAM budget for that peak plus the policy reserve (default 256,000,000 bytes).
- The incoming request's complete context token count at or below the evaluated bound.
- Active requests plus the new request at or below evaluated concurrency, and total context in use plus new context at or below evaluated context times concurrency.
- The configured estimated invocation cost at or below the supplied available budget, both expressed as integer micro-US dollars.

The RAM measurement is the memory budget reserved for the **entire inference workload**, including its resident model. The attested peak must be measured for the declared maximum context and concurrency. It is not merely a model file size, free RAM after loading, theoretical RAM estimate, or total memory on the user's PC. The controller is responsible for measuring the current approved CPU/cloud runtime and for estimating a conservative invocation cost including applicable cloud charges. Zero cost means an explicit measured/approved zero estimate; the module never invents provider prices.

Decisions do not reserve resources or money. Immediately before execution, a trusted controller must atomically reserve the budget and concurrency slots, confirm evidence freshness and actual configuration, enforce timeout/output limits, and reconcile actual usage. This prevents concurrent requests from all admitting against the same stale snapshot. The existing ledger/executor owns those operations; this module is not yet wired into provider dispatch. Cloud autoscaling/deployment limits and CPU-only infrastructure policy must be enforced by deployment configuration. This module has no GPU, provisioning, or experiment execution capability.

## Training and adapter promotion gates

`check_training_permission` returns true only when the policy separately authorizes training and an unexpired `TrainingDataPermission` explicitly permits **training** on that exact dataset digest. Inference permission is insufficient.

`check_adapter_promotion` additionally needs separate promotion authorization and a fresh passing `QuantizedEvaluation` bound to the exact adapter digest, **actual quantized artifact** digest, dataset digest, and capability digest. An evaluation of a different or unquantized artifact is insufficient. The trusted controller must authenticate the permission issuer and evaluator and maintain protected test/evaluation evidence outside model-editable state.

Both helpers return a boolean; neither trains nor promotes anything. Defaults remain disabled. Enabling them is not a recommendation to train: user authorization, data rights, scoped evaluation, and deployment cost controls remain external prerequisites. This implementation does not create an autonomous recursive research loop.

## Integration surface and validation

Construct typed `CapabilityRegistry`, `ModelConfiguration`, and trusted `CapabilityAttestation` records, then create `Router(..., policy=RoutingPolicy(inference_authorized=True))`. Call `route(capability_id, mode=..., inputs=..., facts=..., allowed_effects=(...), context_tokens=..., budget_microusd=..., now=..., resources=...)`. All times are explicit finite Unix seconds. Outcomes are `RoutingDecision(action="route"|"abstain", ...)`, with exact selection, reason, capability/evidence digests, optional measurement digest and escalation reason. `decision.digest` provides a deterministic record hash. No wall clock, filesystem, or network access occurs.

Run `python -m unittest tests.test_routing`. Tests exercise capability contracts and fallback limits, exact model and effort binding, no downgrade, explicit hybrid escalation, cloud-only resource admission, stale/conflicting evidence, budget refusal, and separate dataset permission plus quantized-artifact promotion gates. They do not benchmark Qwen or any provider.
