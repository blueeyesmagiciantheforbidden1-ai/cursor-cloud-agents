# Ryan Frontier: twenty-part implementation and research program

Revision 1 · 21 September 2026

## What this revision actually delivers

The source conversation called for three advances together: frontier research, mathematical depth, and buildable engineering. This revision expands the original five-part organization into twenty substantive parts, and supplies a working Python implementation for the bounded research core. Each part states its mechanism, mathematical obligation, implementation boundary, and next falsifiable experiment.

The deliverable is a research prototype for improving programs and the procedure used to search for them. It is not a demonstrated general-purpose recursively improving intelligence. It has not been installed in the user's command center or either VPS. The code uses the Python standard library so its mechanics can be inspected and run without model credentials or third-party runtime packages. The original Go controller proposal remains a possible production migration; this implementation is Python, not an undisclosed Go build.

The architecture has five pillars, each expanded into four parts:

| Pillar | Parts | Concrete question |
|---|---|---|
| Program semantics | 1–4 | What may change, and how do we establish that it works? |
| Improvement machinery | 5–8 | Can evidence improve the procedure that discovers working code? |
| Scientific and economic evidence | 9–12 | Does the improvement transfer, reproduce, and repay its cost? |
| Execution infrastructure | 13–16 | Can concurrent work remain valid, durable, and bounded? |
| Providers and controlled release | 17–20 | Can the system use available resources and release justified changes? |

A sophistication score of “20” is an ambition, not a measurable benchmark. The measurable targets are accepted quality, counterexample reduction, search cost, transfer to fresh tasks, and total operating cost. More moving parts only earn their place if these measurements improve.

## 1. Define the object that improves

**Research mechanism.** Represent a researcher as \(A=(P,L,M,H)\): a task-solving program \(P\), executable capability library \(L\), improvement method \(M\), and development history \(H\). A research step proposes a descendant \(A'\). The interesting outcome is not merely a successful patch: it is an improved \(M'\) that helps a fresh starting system find useful patches.

**Mathematics.** Let \(\Phi(M,P,L;B,\omega)\) construct a descendant under research budget \(B\). Final evaluation freezes research and measures \(J\) on fresh tasks under a fixed execution allowance. The meta-improvement target is

\[
\Delta_M=\mathbb E[J(\Phi(M',P,L;B,\omega)) - J(\Phi(M,P,L;B,\omega))].
\]

The two methods receive the same starting capability library. Improvement of \(P\) or growth of \(L\) alone is a different claim.

**Implementation.** `research.py` freezes executable search policies learned on development tasks and evaluates their descendants on fresh task batches. It records independent lineage seeds and the development process. The prototype's editable method is a restricted search policy; it cannot rewrite the evaluator or arbitrary controller code.

**Next experiment.** Transfer a frozen method into an unfamiliar workflow family with a fresh empty result cache. Reject the “method improved” claim if the effect disappears without accumulated answers.

## 2. Make the program space explicit

**Research mechanism.** Begin with a small program language for accepting workflow results. A program combines guards over terminal state, fencing, lease validity, ownership, and prior completion. Programs are JSON specifications interpreted by a closed evaluator and compiled into inspectable Python source.

**Mathematics.** The grammar defines a finite hypothesis class \(\mathcal H\). With five optional guards there are at most \(2^5=32\) subsets before restrictions. A candidate's semantics is a function \(h:S\times E\to\{0,1\}\), not a free-form natural-language promise.

**Implementation.** `domain.py` contains the grammar, task generator, interpreter, compiler, and finite state transitions. Generated Python is an artifact built from trusted templates. The system does not execute arbitrary model-returned source. This limited language makes exact within-model comparison practical and makes program changes visible.

**Next experiment.** Increase expressivity one operator at a time—bounded counters, conjunctions with typed predicates, then small control-flow programs. Track how increased expressivity affects solvability, checker expense, and overfitting. Do not jump directly from a five-guard DSL to unrestricted self-modifying Python while retaining the same assurance claims.

## 3. Co-design representations and operators

**Research mechanism.** Search can improve because its representation changes, even when the represented initial behavior does not. A workflow may be represented as guard subsets, decision trees, or dependency-factored rules. A representation migration must carry compatible mutation operators and cached-analysis assumptions with it.

**Mathematics.** For encoder/migration \(m\) and decoder \(d\), require

\[
\forall x\in X_{\rm checked},\quad d(m(p))(x)=p(x).
\]

Equality on a finite explicitly enumerated domain is a bounded certificate. It is not a proof of equivalence for all possible deployed states. Operator quality can then be compared through expected cost-to-first-valid-descendant under equal budgets.

**Implementation.** The shipped DSL provides a canonical, typed program representation, v0-to-v1 migration, and a certificate comparing interpreted and compiled behavior over the full declared finite state/event input domain. General learned representation changes and learned semantics-preserving compiler passes are an extension, not an observed result. The artifact and graph modules provide the identities needed to version such migrations.

**Next experiment.** Compare a bitmask search space with a dependency-factored representation. Keep the initial program semantically identical and include migration, decoding, and validation costs. A useful result would reduce future search work across unfamiliar workflows, not only serialize the same program differently.

## 4. Keep verification independent of the proposal

**Research mechanism.** A candidate proposes acceptance behavior. A separately defined reference workflow and invariant monitor decide whether that behavior satisfies the finite task. The checker searches reachable state/event combinations and returns a concrete violating execution.

**Mathematics.** Bounded correctness is

\[
\forall \tau\in\operatorname{Reach}(s_0,h,H),\quad \operatorname{Spec}(\tau),
\]

where \(H\) is the declared horizon. Progress matters as well as safety: an implementation that refuses every legitimate completion must not win merely by avoiding bad commits.

**Implementation.** `domain.py` reports verification outcome, horizon, visited states, checked transitions, failure kind, and a counterexample. A passing bound that exercised no policy decisions is explicitly ineligible for acceptance. Representation certificates report exact input coverage and distinguish equivalence from workflow safety. Tests exercise generated/interpreted consistency and failure cases. Domain semantics, compiler, and checker are distinct functions, although all live in one reviewable module. Structural separation is not independent human provenance or process isolation.

**Next experiment.** Add a separately authored checker, mutation testing of the evaluator itself, and a real distributed fault-injection harness. Agreement between a finite model and actual software must be measured. A finite-state certificate alone does not prove crash consistency, network behavior, or correctness of SQLite usage.

## 5. Turn failures into search information

**Research mechanism.** Counterexample-guided synthesis uses the failing trace to focus the next proposal. A stale worker completion should focus investigation on fencing and lease semantics, rather than prompt another unconstrained rewrite.

**Mathematics.** Maintain a development counterexample set \(C_t\). A candidate must satisfy all retained examples before an expensive full check. After a failed verification:

\[
C_{t+1}=C_t\cup\{\tau_t\}.
\]

The search budget includes unsuccessful candidates. A counterexample reused in repair is development data, not a protected evaluation sample.

**Implementation.** `research.py` consumes typed verification feedback to learn and compare bounded search strategies. `domain.py` emits traces suitable for deterministic regression checks. Artifact retention makes the underlying evidence inspectable rather than merely recording a model's explanation.

**Next experiment.** Compare random/subset enumeration, counterexample-directed ordering, and trace-minimized ordering. Measure candidate checks, primitive transition checks, elapsed time, and final validity. A directed strategy that performs fewer candidate checks but much more verifier work may be worse economically.

## 6. Give improvement operators contracts

**Research mechanism.** An improvement operator should declare applicability, the program transformation, the invariants it preserves, state migration, and the checks it invalidates. Search operators can themselves become candidates for improvement.

**Mathematics.** Write an operator as \(o=(\mathrm{pre},T,\mathrm{effects},\mathrm{post})\). Composition \(o_2\circ o_1\) is admissible only when \(\mathrm{post}_1\) establishes \(\mathrm{pre}_2\) and the combined effects remain permitted. Semantic compatibility is stronger than “these two patches touch different files.”

**Implementation.** The finite guard operators are typed, enumerable transformations. The graph and transaction APIs can track the resources and contracts a proposed operator depends on. The revised learned method preserves its capability-free proposal sequence until first verified success, then admits memory macros to compete for a simpler valid repair. That guarantees memory cannot delay first success relative to the same revised method without memory in this closed search setting. A separately verified complete-policy fallback occupies the final candidate slot when needed; it uses the known human baseline and is not a learned discovery. General operator contract inference is not implemented; all initial operator semantics are human-authored and checked.

**Next experiment.** Learn applicability classifiers or symbolic preconditions for useful transformations. On a separate task set, record false applicability and abstention rates. Learned preconditions must fail closed outside their validated domain; prediction confidence is not a proof of semantic preservation.

## 7. Improve the research procedure itself

**Research mechanism.** Learn which repair actions to consider and in what order from development evidence, then freeze that method before final evaluation. A method is an executable artifact, not a retrospective narrative about successful repairs.

**Mathematics.** A simple policy \(\pi_\theta(o\mid z)\) orders operators using task features or observed failure class \(z\). The implemented search-policy family is deliberately small. The research objective is quality under the same verifier-call budget, with primitive costs reported separately.

**Implementation.** `research.py` performs development-only method selection, exports frozen method descriptions/source artifacts, and runs the factorial experiment. The initial search space and learning procedure are specified by us. This is a bounded demonstration of method improvement, not discovery of an entirely new learning algorithm.

**Next experiment.** Make operator construction and experimental procedure editable in a second bounded language. Compare one-generation transfer with multiple independent generations. Maintain a fixed trusted evaluator and account for all failed meta-candidates. Hyperagents already investigates editable meta-procedures and transfer; this alone is not a novel literature contribution.

## 8. Compile recurring work into capabilities

**Research mechanism.** Successful reasoning should leave behind something executable: a reusable repair macro, a regression generator, a workflow procedure, or a typed tool wrapper. Reuse is allowed only when its assumptions apply.

**Mathematics.** A capability \(c\) maps inputs satisfying \(\mathrm{pre}_c\) to outputs satisfying \(\mathrm{post}_c\), within an effect budget. Capability acquisition changes \(L\); it must not be mistaken for an improvement to \(M\). Keep these two axes experimentally separate.

**Implementation.** The research experiment includes a development-derived capability condition and a baseline-capability condition. The compiler exports actual program source from accepted bounded candidates. The four factorial arms expose whether an apparent gain comes from a learned method, reusable repairs, or their interaction.

**Next experiment.** Extract larger procedures from traces, then test them on structurally unfamiliar repositories. Track applicability coverage, correctness, fallback frequency, and maintenance cost. The local-model adaptation branch remains future work: this release trains no neural weights or adapters and does not claim frontier performance from a small local model.

## 9. Use a causal transfer experiment

**Research mechanism.** Run the original and improved methods with both original and accumulated capabilities. Use copies of the same independent starting unit to reduce nuisance variation, while keeping independent units separate.

| | Original capability library | Development-derived capability library |
|---|---|---|
| Original research method | Baseline | Capability effect |
| Improved research method | Method transfer | Combined effect |

**Mathematics.** Let arm means be \(\mu_{00},\mu_{01},\mu_{10},\mu_{11}\). The method contrast is \(\mu_{10}-\mu_{00}\), capability contrast is \(\mu_{01}-\mu_{00}\), and interaction is

\[
I=\mu_{11}-\mu_{10}-\mu_{01}+\mu_{00}.
\]

An interaction is not itself evidence of a method-only gain.

**Implementation.** `research.py` runs the factorial arms on held-out task batches and reports an additional shifted condition. Each independent lineage includes its own development process and may select a different frozen method. Thus the interval estimates the development-and-selection pipeline, not one universally fixed learned method. Tasks from one descendant are not counted as independent research lineages.

**Next experiment.** Transfer across semantics, not only seeds: different retry rules, resource ownership models, language frontends, and repository structures. The current synthetic conditions establish a reproducible experiment, not broad industrial transfer.

## 10. Separate exploration from confirmation

**Research mechanism.** Development chooses candidates. A separately frozen confirmation protocol estimates whether an improved method meets predefined margins. Inconclusive evidence must remain inconclusive.

**Mathematics.** For paired quality differences \(D_i\in[-1,1]\) across \(n\) independent units, with \(q\) predeclared comparisons, a simultaneous two-sided Hoeffding radius is

\[
\epsilon_n=\sqrt{\frac{2\log(2q/\alpha)}{n}}.
\]

Require \(\bar D-\epsilon_n>\delta\), where \(\delta\) is a margin chosen before confirmation. For a 0.05 radius, \(q=12\), and \(\alpha=0.05\), the bound needs 4,940 independent units. Large task batches within one unit do not solve this sample-size problem.

**Implementation.** `research.py` computes conservative paired bounds and does not promote a small pilot as statistically conclusive. `promotion.py` refuses inconclusive proposals. The included runs are inspectable pilots with a declared family of comparisons.

**Next experiment.** Preregister a sufficiently powered comparison after pilot work. If candidate selection or stopping adapts to results, use fresh confirmation data or a valid sequential procedure such as an appropriately designed confidence sequence. Repeatedly peeking at fixed-sample bounds and stopping when they look favorable is invalid.

## 11. Account for full research economics

**Research mechanism.** Charge improvement for discovery, failed trials, verification, migration, and upkeep. Report quality, money, elapsed time, memory, and primitive computation separately unless a justified conversion exists.

**Mathematics.** At comparable quality, if one-time total improvement cost is \(R\) and net saving per future task is \(s>0\), break-even reuse is \(N>R/s\). If \(s\le0\), there is no monetary break-even. Under a vector budget \(B\), all constrained dimensions must remain feasible.

**Implementation.** The pilot reports verifier calls and primitive checking work separately, and includes development costs. `ledger.py` uses integer microdollars for real invocation reservation and reconciliation. The offline demonstration spends no model credits; this does not establish the costs of a live frontier-model version. Wall-clock measurements are machine-dependent.

**Next experiment.** Measure useful completed tasks per actual subscription allowance consumed and per API dollar. Compare equal primitive-verification budgets as well as equal candidate-check budgets. The current candidate-call budget is not a claim of equal wall time, equal computational work, or equal total research investment: learned arms receive extra development and selection work that the baseline does not. That expense is reported in lifecycle totals; an equal-total-cost efficiency experiment remains future work.

## 12. Preserve evidence and lineage

**Research mechanism.** Every accepted or rejected experiment should identify its task inputs, candidate program, method, evaluator, environment, and costs. Content hashes let another process retrieve the exact object used.

**Mathematics.** A provenance node is a content-addressed function of canonical inputs. A hash collision is assumed infeasible; a matching hash establishes identity, not scientific validity. Pareto dominance requires a candidate no worse on every declared objective and strictly better on at least one.

**Implementation.** `artifacts.py` stores content by SHA-256 with atomic writes and read-time integrity checks. The research report retains lineage records and a bounded experimental archive. `promotion.py` binds candidate, evaluator, and evidence identities at approval and activation.

**Next experiment.** Reproduce a run from a clean machine with pinned interpreter, task generator, evaluator, and configuration. Add a software bill of materials, signed manifests, independent storage, and retention policy before production. Same-seed reproducibility does not imply independent evidence.

## 13. Treat the project as a dependency graph

**Research mechanism.** Code, requirements, tool versions, prompts, schemas, and reviews are versioned resources. Derived facts and checks depend on particular versions. When an input changes, invalidate the affected descendants.

**Mathematics.** For dependency graph \(G=(V,E)\), updating resource \(v\) invalidates its reverse-transitive dependent closure. A cached result remains eligible only when its recorded input versions match current versions. Dependencies are assumptions the system must record; omitted edges create unsound reuse.

**Implementation.** `graph.py` supports resource versions, dependency registration, cycle rejection, cache invalidation, snapshots, and serialization. Requirements can use the same resource identities as files. The current graph is a controller utility, not a complete repository analyzer or automatic dependency inference engine.

**Next experiment.** Compare full recomputation with dependency-guided recomputation after controlled changes to code, contracts, and requirements. Inject missing dependencies to measure detection limits. Retain repository integration checks because conservative graph tracking cannot establish all semantic dependencies automatically.

## 14. Give parallel work transaction semantics

**Research mechanism.** A candidate records the versions it read and the resources it intends to change. Integration validates those versions before committing proposed outputs.

**Mathematics.** Conservative conflict detection between candidates \(i,j\) checks

\[
W_i\cap(R_j\cup W_j)=\varnothing,\qquad W_j\cap R_i=\varnothing.
\]

Read-version validation remains necessary even when there is no pairwise file conflict. API contracts and requirement identities are resources too.

**Implementation.** `graph.py` exposes snapshot validation and tracked transactions. Tests cover stale dependencies and conflicts. Independent agents in the development workflow own separate files, but production integration must validate declared semantic read/write sets as well.

**Next experiment.** Schedule conflicting and non-conflicting candidates under contention and measure wasted work, accepted throughput, and escaped regressions. Speculative work may create artifacts; irreversible external actions remain under the controller and cannot be “rolled back” by reverting a Git patch.

## 15. Make execution durable and spending fenced

**Research mechanism.** A durable controller issues monotonically increasing fencing tokens with worker leases. Every paid invocation reserves budget before dispatch. Unknown billing outcomes stay reserved until reconciled.

**Mathematics.** Accepted result transition requires a live current fence. Available budget is the configured limit minus settled spend minus outstanding reservations. Retries may duplicate computation in an external provider, but must not grant a second local dispatch authorization for the same invocation identity.

**Implementation.** `ledger.py` uses SQLite WAL and transactions for task creation, claim/reclaim, reservation, uncertain-charge tracking, settlement, and accepted completion. Stale workers can report genuine late costs without gaining result-commit authority. If actual cost exceeds a reservation, accounting records the truth and blocks further spending rather than hiding the overage.

**Next experiment.** Add a durable outbox and provider idempotency integration, then fault-inject crashes between reservation, dispatch, reply, settlement, and completion. The current local ledger does not magically guarantee exactly-once execution inside an external API. Its lease and reservation rules are the foundation for a transport implementation.

## 16. Choose computations under bounded budgets

**Research mechanism.** The controller can retrieve evidence, run a test, propose a repair, request an independent check, reuse a capability, or stop. In the prototype, this choice is a bounded search schedule. A learned value-of-computation policy is a future extension.

**Mathematics.** A possible selection objective is

\[
\operatorname{VOC}(a\mid D)=R(D)-\mathbb E[R(D\cup Y_a)]-\lambda_C C(a)-\lambda_T T(a).
\]

This is useful only if the estimated loss reduction is calibrated. Model confidence is not a calibrated estimate by default. The controller's own decision cost belongs in the budget.

**Implementation.** Candidate generation and method evaluation have explicit budgets. Exhaustion is a recorded outcome, not an invitation to retry indefinitely. Domain horizons and finite grammar cap verification. The delivered system does not implement a general recursive context agent, learned VOC estimator, or broad repository scheduler.

**Next experiment.** Compare simple heuristics against learned action selection on logged reproducible tasks, then controlled reruns. Counterfactual performance of an action never taken cannot be inferred reliably from ordinary execution logs alone.

## 17. Route useful work across available providers

**Research mechanism.** Preserve the user's preference for each provider's strongest approved model and maximum supported effort. Balance work across eligible allowances without silently replacing quality with a weaker model. Keep the ChatGPT connection reserve separate.

**Mathematics.** Among eligible providers, a simple fair policy minimizes normalized prospective usage, adjusted by configured weight. Capacity units must be comparable within each allowance policy; API cash is a separate constraint. Freeze the routing policy hash during a controlled experiment.

**Implementation.** `routing.py` is a verified-capability planner with explicit abstention. `examples/providers.example.json` includes Grok, Cursor, Claude, Copilot, and reserved ChatGPT entries, all unverified initially. Unknown or unverified capabilities do not dispatch work. The planner does not invoke these applications or prove that a configured model is globally best.

**Next experiment.** Connect supported native CLI/agent interfaces on the actual VPS hosts, add real usage reconciliation, and test one account at a time. Alibaba Gateway may aggregate authorized API routes; it does not merge subscription plans or proprietary model weights. Provider model IDs and effort settings require account-specific verification.

## 18. Separate editable intelligence from authority

**Research mechanism.** Researchers may change task-solving code, search policies, and capability proposals. They must not be able to rewrite the authoritative budget, evaluator, held-out data, permissions, or approval rules while being evaluated.

**Mathematics.** Give a candidate an effect set \(E_c\) constrained by a controller-issued grant \(G\): \(E_c\subseteq G\). An artifact may request additional effects; requests confer no authority. Grant identity must include task fence and applicable budget reservation for paid operations.

**Implementation.** The prototype confines candidates to a closed grammar and never runs untrusted shell or arbitrary Python. Controller utilities and promotion registry remain outside the candidate representation. This is an intentionally narrow execution boundary, not an OS-level sandbox for arbitrary repository agents.

**Next experiment.** Before unrestricted code synthesis, add isolated non-root workers with constrained filesystem, network, resources, and credentials. Do not mount the controller database, holdout corpus, or host Docker socket inside candidate execution. Test attempted evaluator modification and credential access as concrete boundary checks.

## 19. Release exact, justified versions and retain rollback

**Research mechanism.** Development produces a proposal. Sufficient evaluation produces eligibility for review. Approval applies to one exact candidate and its exact evidence, followed by activation. These are separate state transitions.

**Mathematics.** A release is valid only if the candidate, evaluator, and evidence hashes match the approved tuple and the predefined lower-bound criterion exceeds its margin. A rollback references a previously activated release; it is not an arbitrary new unreviewed candidate.

**Implementation.** `promotion.py` persists proposals and releases, rejects inconclusive evidence, binds approval and activation to immutable identities, and records rollback history. It trusts the controller calling it. A caller-supplied approver name is an audit field, not authentication; identity verification belongs in a future service boundary.

**Next experiment.** Add authenticated approvers, evidence revocation, canary traffic, regression thresholds, and crash-safe deployment. Automatically rolling back mutable database schemas or irreversible external effects requires separate migration design. The prototype does not imply that all effects can be reversed by changing an artifact pointer.

## 20. Evaluate the whole system and graduate in stages

**Research mechanism.** Measure the integrated system against a strong single-agent baseline and simpler search baselines. Ablate each expensive mechanism to determine whether it improves outcomes enough to justify itself.

**Mathematics.** Predeclare a result vector such as accepted quality, regressions, total research spend, task spend, latency, peak memory, and maintenance effort. Optimize a Pareto frontier until the application provides defensible scalar weights. Include the cost of development and independent verification.

**Implementation.** The command-line interface runs a complete local demonstration, a multi-lineage pilot, and routing inspection. A fixed all-guards-first comparator guards against overstating gains over the weaker breadth-search baseline; the current monotone domain admits a universally correct known guard set. Tests cover domain behavior, search accounting, durable ledger failures, content integrity, graph invalidation, provider selection, and promotion rules. Example reports and generated programs are packaged with source. The real test count and pilot results are recorded after execution in `VALIDATION.md`.

**Next experiment.** Graduate through: (1) reproducible finite-domain run; (2) isolated repository patch repair; (3) actual two-VPS controller transport; (4) one approved provider integration; (5) multi-provider comparison; (6) sufficiently powered method-transfer study; (7) successive-generation replication. Local neural adaptation and arbitrary self-editing remain later branches requiring their own evidence.

## The proposed frontier contribution

A defensible research question is whether **jointly improving the representation, transformation operators, and experiment-selection procedure reduces the full cost of future discoveries on unfamiliar systems, after accounting for migration and verification**. The supplied prototype provides an executable measurement base. It does not yet establish that result.

Existing work already covers important pieces: DGM explores empirically evaluated self-modifying coding agents; Hyperagents explores editable meta-procedures and transfer; AlphaEvolve demonstrates evaluator-guided program improvement; GEPA uses reflective feedback for optimizing language-model programs. The combination must outperform appropriate baselines under controlled experiments to count as an advance. See the primary-source notes in `RESEARCH_SOURCES.md`.

## What is intentionally not claimed

No frontier-model weights were merged. No local model was trained. No general self-improvement theorem was proven. No production repository, computer desktop, Alibaba account, subscription application, or VPS was modified. No small pilot is presented as statistically conclusive. These boundaries identify the next engineering and scientific work precisely, while the delivered finite-domain implementation remains executable now.
