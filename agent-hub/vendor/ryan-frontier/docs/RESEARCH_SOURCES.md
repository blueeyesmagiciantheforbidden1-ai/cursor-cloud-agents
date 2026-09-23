# Research evidence, design review, and provider corrections

Reviewed 21 September 2026. This is a source-grounded research review for Ryan Frontier. Reported results below belong to the cited authors; none are results produced by this repository. The proposed extensions require experiments. Provider documentation describes available product surfaces, not access already configured for this user.

## Eight core primary sources

| Source | What the source establishes | Design consequence | What it does not establish |
| --- | --- | --- | --- |
| [1. Darwin Gödel Machine, Zhang et al.](https://arxiv.org/abs/2505.22954), submitted 2025; current version revised 12 March 2026 | The authors generate self-modified coding agents, retain an archive, and evaluate changes empirically on coding benchmarks. | Retain lineage and useful diverse candidates; compare to a no-archive baseline. | A proof that arbitrary self-modification helps, an unlimited improvement trajectory, or production reliability. |
| [2. HyperAgents, Meta research publication](https://ai.meta.com/research/publications/hyperagents/), 24 March 2026 | The task agent and meta agent are one editable program. The authors report evolving the improvement procedure, cross-domain transfer, and accumulation across runs. | Treat editable search procedures and transfer as prior art. Test a more specific contribution. | A universal theorem of accelerating intelligence or proof that this proposed implementation will transfer. |
| [3. AlphaEvolve, Google DeepMind](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/), 14 May 2025 | Model-generated programs are checked and scored by automated evaluators within an evolutionary program database. The team reports algorithmic and infrastructure improvements. | Begin in domains with independent, inexpensive, trustworthy evaluation; count verification expense. | A sound verifier for arbitrary software or permission to let candidates redefine their own success. |
| [4. GEPA, Agrawal et al.](https://arxiv.org/abs/2507.19457), submitted 25 July 2025 | Reflection over execution trajectories proposes prompt changes and combines complementary candidates from a Pareto frontier. | Use structured failure traces and a feedback-driven prompt baseline before expensive representation or model learning. | Guaranteed domination of reinforcement learning on all tasks, or a demonstration of this system's recursive transfer. |
| [5. Time-uniform, nonparametric, nonasymptotic confidence sequences, Howard et al.](https://arxiv.org/abs/1810.08240), Annals of Statistics 2021 | Confidence sequences provide coverage across time under their stated statistical assumptions. | Use a valid sequential procedure if observing interim results may determine when to stop. | Validity under arbitrary data leakage, dependent pseudo-replication, changing outcomes, or post-hoc candidate revision. |
| [6. Alibaba AI Gateway: Create a service](https://www.alibabacloud.com/help/en/api-gateway/ai-gateway/user-guide/creating-services), updated 21 June 2026 | The gateway supports named providers, configurable endpoints, OpenAI-compatible or native protocols, and separately obtained provider credentials. | An Alibaba gateway can unify approved API routes; protocol and capability compatibility must be tested. | Merged model weights, merged subscriptions, a common upstream invoice, or availability of every feature through protocol translation. |
| [7. Alibaba Model Studio: Coding Plan](https://www.alibabacloud.com/help/en/model-studio/coding-plan), updated 11 September 2026 | Plan keys and endpoints differ from pay-as-you-go keys. The current page restricts the plan to interactive tool use and explicitly excludes automated scripts, backends, and other non-interactive uses. | The autonomous research backend needs an eligible API route; do not power it with a Coding Plan key. | A subscription-backed allowance for unlimited unattended research workers. |
| [8. Claude Code: Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance), checked 21 September 2026 | Ordinary use of native Claude Code can use subscription authentication; the page distinguishes it from developer API use and bars third-party credential/token intermediation. | Keep an approved native CLI worker separate from a gateway API adapter. Store eligibility by product, account, authentication method, and intended use. | Permission to collect subscription tokens into a generic proxy or assume every automation pattern is covered. |

The evidence for editable meta-procedures is stronger than the pasted draft acknowledges. Its claim that all named systems stop short of transferable research procedures should be removed: source 2 explicitly reports transfer of meta-level improvements. A meaningful research claim must specify the difference from that existing result.

## Proposed contribution, clearly separated from evidence

The proposal is **budgeted co-design of program representation, search operators, and evaluation accelerators**, with certified migration in a restricted language and causal transfer tests into unfamiliar starting researchers. Each clause adds a falsifiable requirement:

1. **Representation:** The system changes the coordinates in which it searches, not merely the text of one candidate. It must preserve the starting program's semantics under a declared migration.
2. **Operator:** The system learns reusable transformations, including applicability predicates and effect summaries, rather than retaining task answers as if they were a general method.
3. **Evaluation accelerator:** The system learns sound pruning, dependency analysis, or incremental checks. An independent authority checks their certificates; the candidate never replaces the final evaluator.
4. **Transfer:** The exported method helps fresh starting researchers under equal budgets, without exporting private test answers, hidden evaluation traces, or result caches.
5. **Economics:** Benefits survive charging failed searches, migration, validation, execution, and maintenance. Quality, dollars, latency, and memory remain separate axes unless a conversion was declared beforehand.

This combination is a proposed research direction, not a verified novelty claim. A proper related-work review of individual subproblems remains necessary before asserting publication-level novelty.

## Mathematical checks for the blueprint

### Experimental unit and causal target

Let a research procedure `M` run from initial researcher `A` and initial capability library `L` with research budget `B`, producing descendant `D = Research(M, A, L; B)`. Evaluate `D` on fresh tasks under a fixed execution budget with further research disabled. One independently sampled research lineage includes the initial state, training tasks, execution randomness, and its evaluation batch.

For paired unit `i`, both arms start from copies of the same initial state and declared common randomness where applicable. Write normalized batch score difference `X_i = Q(D_new,i) - Q(D_base,i)`, so `X_i` lies in `[-1, 1]`. The estimand is the mean of these lineage-level differences under the declared sampling distribution. A thousand tasks scored on one descendant are a thousand task observations but only one research lineage. If independent lineages share an adaptive archive, they cease to be independent in the required sense; reset that archive or analyze the dependence explicitly.

The minimum factorial ablation is:

| | Factory capability library | Accumulated capability library |
| --- | --- | --- |
| Original research method | Baseline | Benefit of retained capabilities |
| Revised research method | Transfer of method | Combined system |

Exported learned parameters must be declared as part of the method. The method-only arm starts without experimental memory, answer caches, or undeclared learned artifacts. Add fixed-method search, prompt-only optimization, and a manually designed operator baseline when each addresses the proposed mechanism.

### Fixed-horizon multiplicity

For `K` frozen candidate methods, `C` declared conditions, and `n` independent paired lineage units per comparison, Hoeffding's inequality and a union bound yield the simultaneous radius

```text
epsilon(n) = sqrt(2 * log(2*K*C/alpha) / n).
```

The factor 2 inside the square root comes from the range width of `X_i`, which is 2. For each comparison, the two-sided tail is at most `2 exp(-n epsilon^2 / 2)`. Independence is required across units within a comparison; comparisons can share units because the union bound does not require independence between comparisons.

For `K=4`, `C=3`, `alpha=0.05`, and `epsilon=0.05`, the sufficient count is `ceil(2 log(480)/0.05^2) = 4,940` independent lineage units per comparison. This is a conservative guarantee, not a practical default experiment size and not an a priori power calculation. A pilot can estimate paired variability and plausible effect sizes, followed by a separately specified confirmatory design.

Declare required margins `delta_c` first. Promote only when the lower confidence bound exceeds the required margin in every mandatory condition. If one condition permits a small regression, encode its margin as negative in advance. Do not silently average away a serious regression in one domain.

### Optional stopping and multiple generations

Repeatedly inspecting ordinary fixed-horizon intervals and stopping when they become favorable invalidates the advertised coverage. A conservative, elementary alternative allocates failure probability over time and generations. With fixed candidates within generation `g`, take `w_g = 1/[g(g+1)]` and `v_n = 1/[n(n+1)]`. Because both weight sequences sum to 1, a radius

```text
epsilon(g,n) = sqrt(2 * log(2*K*C / (alpha*w_g*v_n)) / n)
```

gives a union-bound guarantee across all declared comparisons, looks, and generations, provided each generation uses fresh data satisfying the assumptions after conditioning on its development history. This is an original elementary derivation from bounded-variable concentration; it is not presented as a formula from source 5. It is intentionally conservative. A sharper confidence-sequence method from source 5 is a later improvement only after its assumptions and implementation have been checked.

Any candidate revised after observing confirmation results becomes a new candidate and needs fresh confirmatory data. Repeatedly using the same hidden set consumes its scientific usefulness even if filesystem permissions hide the files. A sequential interval solves optional stopping, not adaptive benchmark contamination.

### Representation preservation and computational cost

In a finite workflow DSL with reference semantics `Sem`, encoding `E`, migration `T`, and decoder `D_new`, require

```text
Sem(D_new(T(E(p)))) = Sem(p)
```

over the declared finite input/state domain. This preserves the starting behavior; it says nothing by itself about whether future search becomes better. That requires separate equal-budget experiments. A bounded counterexample check proves only the stated finite or bounded property. It does not prove liveness for an unbounded deployed distributed system.

For an explicit finite transition graph with `S` reachable states and `E` transitions, complete graph exploration is typically `O(S+E)` time and `O(S)` storage, excluding transition-construction costs. If `d` state variables have domain sizes `m_j`, the naive upper bound is `S <= product(m_j)`, so apparently small models can still grow exponentially in the number of variables. Learned dependency slicing must justify omitted variables, not simply stop exploring them. Every cache must key on all semantic dependencies and invalidate when those dependencies change.

### Economic claim

At matched quality, let `C_discovery` include every research and validation cost, and `s` be net expected savings per future task after extra maintenance and inference costs. The approximate break-even reuse count is `C_discovery / s` only if `s > 0`. If savings are uncertain or workload changes, report an interval or scenario analysis. A better score at a larger budget is not evidence that the research algorithm became more efficient.

## Twenty architecture recommendations for the main specification

These are review recommendations, not a declaration that all twenty components have been implemented.

| Group | Part | Review requirement |
| --- | --- | --- |
| Authority and execution | 1. Immutable supervisor | Keep permission, promotion, accounting, and final-evaluation authority outside candidate write access. |
| Authority and execution | 2. Durable task ledger | Record idempotent state transitions and recovery evidence; model retries explicitly. |
| Authority and execution | 3. Lease and commit authority | Fence stale workers with monotonic tokens; a stale computation cannot commit or authorize new spend. |
| Authority and execution | 4. Budget reservation and reconciliation | Reserve before dispatch; distinguish charged, estimated, reserved, and unknown costs; retain uncertainty after timeouts. |
| Provenance and interfaces | 5. Content-addressed artifacts | Hash code, prompts, dependencies, input sets, environment, and evaluator versions. |
| Provenance and interfaces | 6. Provider capability registry | Pin exact model, endpoint, effort, tool support, auth method, and permitted usage; freeze routing during experiments. |
| Provenance and interfaces | 7. Snapshot and lineage graph | Record all parent artifacts and research state; make branch and restart semantics explicit. |
| Provenance and interfaces | 8. Evidence and data-rights ledger | Preserve sources and distinguish permission for inference, retention, sharing, training, and redistribution. |
| Program invention | 9. Typed workflow representation | Start with a small language whose state and effect semantics can be independently interpreted. |
| Program invention | 10. Migration and decoding certificates | Require semantic preservation of the starting program before evaluating a new representation's search advantage. |
| Program invention | 11. Operator library | Store applicability, constructor, effects, pre/postconditions, counterexamples, and invalidation dependencies. |
| Program invention | 12. Counterexample synthesis loop | Turn verifier failures into bounded repair tasks; keep development feedback distinct from confirmation. |
| Research and selection | 13. Diverse candidate archive | Bound storage and record selection policy; compare diversity-aware search against an archive-free baseline. |
| Research and selection | 14. Editable research procedure | Allow search scheduling and proposal logic to evolve in a sandbox, without editing its authority or score definition. |
| Research and selection | 15. Bounded computation selector | Estimate the value of another search/check from calibrated evidence and cap selector overhead. |
| Research and selection | 16. Local capability progression | Prefer deterministic reusable operations, then retrieval/tools, then a separately authorized training experiment. |
| Measurement and deployment | 17. Independent evaluation service | Own hidden partitions, reference semantics, resource accounting, and checks of learned certificates. |
| Measurement and deployment | 18. Transfer and ablation harness | Sample fresh research lineages, separate methods from accumulated capability, and freeze equal budgets. |
| Measurement and deployment | 19. Statistical promotion service | Enforce declared outcomes, margins, multiplicity, stopping rules, and dataset retirement. |
| Measurement and deployment | 20. Release, rollback, and economic review | Stage changes within their validated domain; record rollback triggers and total amortized costs. |

## Corrections to the pasted integration advice

| Pasted claim or implication | Verified correction / implementation treatment |
| --- | --- |
| All existing self-improvement research stops short of transferable research methods | Incorrect as a blanket statement. HyperAgents explicitly reports meta-level transfer. State the narrower proposed contribution above. |
| Alibaba merges the models | AI Gateway routes calls among provider services. Shared routing does not combine proprietary weights or entitlements. Source 6 supports the routing architecture. |
| Every provider can use the same feature surface | OpenAI-compatible transport is not a guarantee of equivalent tools, streaming events, structured output, reasoning controls, or usage reporting. Each adapter needs a capability contract and integration check. |
| New-looking Qwen IDs in the draft are necessarily invented | The [current official Model Studio catalog](https://www.alibabacloud.com/help/en/model-studio/models), updated 21 September 2026, actually lists `qwen3.7-plus`, `qwen3.8-max`, and `qwen3.8-flash`. Catalog existence does not establish account, region, plan, or gateway availability. Defaults should remain unconfigured until a live eligible identifier is selected. |
| A Coding Plan can fuel the unattended research backend | Source 7 explicitly excludes automated scripts and backends. Use an eligible pay-as-you-go API route for that workload. |
| Native CLI subscriptions and API keys are interchangeable | They are different integration/authentication surfaces. Native CLI support must be checked per vendor and intended use. Never convert session tokens into shared API credentials. |
| Claude subscriptions cannot support any CLI automation | Too broad. The [official programmatic usage page](https://code.claude.com/docs/en/headless) documents `claude -p`; its bare mode uses API/provider credentials rather than subscription login. Source 8 distinguishes native product use from developer service intermediation. The backend must record the actual supported path. |
| “Never Beijing,” or “Beijing URLs will fail because you are in Jamaica” | The cited materials do not establish that universal claim. Select the endpoint matching the account, region, model and provider terms. Geography alone is not a sufficient diagnosis of an authentication error. |
| A serverless gateway in a specific region always costs a small amount, while dedicated is about $1,000/month | Not verified in this review. Do not turn these historical estimates into deployment defaults. Record region, SKU, availability, network requirements, and a current account quote before provisioning. |
| The shown gateway hostname, wildcard alias syntax, and UI click path are deployment-ready | These were not verified against the user's actual console. Use configuration placeholders and a deploy-time validation checklist, not an invented instance hostname. |
| A 401 identifies the exact configuration failure | Authentication errors require provider response and request context. A stale model ID, wrong protocol, missing entitlement, wrong endpoint, or invalid credentials need not produce the same status code. |
| Fallback to a cheaper model preserves an experiment | Changing the provider/model changes the arm unless the fallback is part of the frozen policy. Record failover as an outcome or rerun under a predeclared policy. |
| Reported zero usage cost must always be a bug | Zero may be legitimate for a local operation or a documented allowance; missing or unknown billing must never be represented as zero. Separate measured resource use from marginal cash charges. |
| Permission to call a model grants permission to train another model on its outputs | No such inference is justified. Data provenance, licenses, applicable terms, consent, and allowed training uses require separate records. Eligibility cannot be inferred from possession of a subscription. |

The current review does not validate exact OpenAI, xAI, Cursor, or Copilot model identifiers, account entitlements, cloud instance prices, or the user's API connectivity. Those should remain explicit configuration inputs rather than asserted working integrations.

## Main failure modes to falsify in experiments

- **Evaluator replacement:** A candidate improves its apparent score by modifying tests, accounting, sampling, or timeout interpretation. Remedy: separate authority, immutable evaluator identity, and independent scoring.
- **Answer transport:** Method transfer works only when caches or accumulated task answers accompany it. Remedy: factory-library factorial arm and inspection of the exported method artifact.
- **Pseudo-replication:** Many tasks from one descendant create spuriously narrow intervals. Remedy: count independent research lineages and publish the sampling hierarchy.
- **Survivorship:** Only successful research spend is counted. Remedy: complete lineage-level cost accounting, including failed and abandoned candidates.
- **Selection leakage:** The confirmation set becomes development feedback. Remedy: separate selection and confirmation stages, retire exposed partitions, and include future attempts in the error budget.
- **Model substitution:** Improvements come from a stronger provider or larger reasoning allowance. Remedy: freeze provider configuration and compare equal full budgets.
- **Verifier shortcut:** An evaluation accelerator omits relevant states. Remedy: independently checked dependency certificates and fallback to the reference evaluator.
- **Scope inflation:** A finite-state result is described as production distributed-systems correctness. Remedy: state bounded semantics and test the implementation/refinement boundary separately.
- **Economic mirage:** Lower serving cost never amortizes the research expense. Remedy: disclose break-even reuse and workload assumptions alongside quality.

