# Improvement report: tested Codex package

The final revision passed the declared engineering comparison: **384/384 bounded validation tasks solved, versus 263/384 for the original version on the same tasks** (68.49% → 100.00%). No previously solved task was lost. **100 tests passed.**

This is an improvement to a finite-domain research prototype. It is not a certified “10/10,” a new frontier algorithm, or a production deployment. The known complete-guard baseline also solves all tasks. Its final-slot use explains the reliability gain and is explicitly recorded.

## What changed through the iterations

| Revision | Concrete change | Evaluation outcome | Disposition |
|---|---|---|---|
| 0 | Initial learned search plus memory-first capability reuse | Development pilot exposed memory displacing useful search | Preserved as baseline |
| 1 | Learned methods admitted reusable repairs only after first verified success | On its fresh comparison: 259/384 → 313/384, but 2 previously solved tasks were lost | Failed no-loss gate; retained and retired into development evidence |
| 2 | Final-slot independently checked complete-policy fallback, within the original six-candidate allowance | On a different fresh comparison: 263/384 → 384/384, zero lost cases | Passed the engineering gate |

The two 384-task comparisons use different declared seeds. Do not treat revision 1's 313 and revision 2's 384 as a paired comparison. Revision 2 is paired with revision 0 on its own fresh tasks.

The composition and fallback changes were manually engineered in response to observed failures. Search ordering and failure rules are learned from each lineage's development experiments. The report does not conflate our engineering work with an autonomous scientific discovery.

## Final paired comparison

| Seed | Condition | Original combined method | Revised combined method | Previously solved cases lost |
|---|---|---:|---:|---:|
| 20260924 | fresh | 81/96 | 96/96 | 0 |
| 20260924 | shift | 62/96 | 96/96 | 0 |
| 20260925 | fresh | 72/96 | 96/96 | 0 |
| 20260925 | shift | 48/96 | 96/96 | 0 |

The protocol used two fixed seeds, 32 independently developed lineages per seed, three final tasks per lineage per condition, two conditions, and six candidate/verifier slots per task and arm. That is **64 independent lineage units and 384 task cases**, not 384 independent research lineages. Original control arms and the fixed human baseline retained their outcomes. The revised learned method with and without memory agreed on success and first-success timing.

Full source and evidence are retained in `examples/revision-0`, `examples/revision-1`, `examples/revision-validation`, and `examples/revision-protocol.json`. `scripts/compare_revisions.py` reproduces the final comparison. Replaying those seeds produces reproducibility evidence, not new confirmation data.

## Why the correction works within this domain

Before its first accepted repair, the revised method ignores capability macros. Therefore its proposal sequence matches the same revised method without memory up to first success. After success, it retains the verified incumbent while considering possible simplifications.

If no candidate has passed by the last slot, it checks the known complete guard policy in that slot, provided that policy has not already been checked. This consumes real verification work and no additional candidate slot. The guarantee is conditional on that complete policy satisfying the task with nonvacuous bounded verification. That condition holds for this finite generated family; it is not assumed for arbitrary software.

The all-guards baseline is already known and can succeed immediately. This revision does not demonstrate lower search cost or better quality than that baseline. Its value is reliable composition, explicit accounting, executable learned-method artifacts, and an auditable improvement loop. Future research must establish benefits on harder, unfamiliar problem families.

## Additional defects corrected

- Divergent restored dependency graphs can no longer accept each other's snapshots merely because their numeric versions match. Snapshots bind resource revision identities.
- A bounded check that exercised no candidate policy decisions cannot qualify as an accepted solution.
- Representation certificates expose exact finite input coverage and source/target/compiled identities, and state that equivalence is separate from safety.
- Release identity binds the full pipeline, design, frozen method records, and executable bytes.
- Statistical and non-regression release gates are recomputed and persisted without rewriting unfavorable confidence bounds.
- Per-run reports and receipts remain immutable; latest-file views update atomically.
- Known offline failures settle zero cash cost and record failure while preserving the original exception.

## Scientific status and costs

The example eight-lineage run still reports **inconclusive**, leaves promotion **inconclusive**, and activates no release. Engineering regression checks passing is separate from a statistically justified research-method promotion.

The example run counts 2,336 verifier calls, 25,386 checked states, 48,069 checked transitions, and 29,333 candidate policy decisions, including development and failed work. It makes zero provider calls. Candidate budgets are equal during final evaluation; total research cost is not matched between learned and original arms.

No VPS, existing command-center repository, model subscription app, or Alibaba service was modified. The package's next integration boundary is documented in `docs/DEPLOYMENT.md` and the ready-to-paste `CODEX_PROMPT.md`.
