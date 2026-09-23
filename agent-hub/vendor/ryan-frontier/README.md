# Ryan Frontier

A runnable, offline prototype for improving small programs and the procedure used to discover them. It expands the original five-part proposal into twenty parts covering research, mathematics, and implementation.

**Start with [the twenty-part architecture](docs/FRONTIER_20.md).** The code demonstrates bounded synthesis, counterexample feedback, development-only method selection, executable policy export, fresh-task transfer experiments, durable accounting, and controlled release records.

## Run

Python 3.11 or newer on Linux/POSIX (including WSL2). No third-party runtime packages or API keys required. Run these commands from this directory:

```bash
python3 -m unittest discover -s tests -v
python3 -m ryan_frontier demo --output runs/first
python3 -m ryan_frontier pilot --units 8 --budget 6 --output runs/pilot
python3 -m ryan_frontier route --config examples/providers.example.json
```

The demo writes `summary.json`, a detailed `report.json`, generated Python programs and specifications, verified content-addressed objects, and local SQLite records. Re-running in the same output directory creates another task, preserves an immutable per-run report and receipt, and updates atomic latest-file views while retaining prior objects and ledger history. Use a fresh directory to keep a separate named run.

The router abstains with the supplied unverified template. It makes no model calls. The template's allowance values are illustrative, not real account quotas.

## What is implemented

| Module | Responsibility |
|---|---|
| `domain.py` | Typed workflow policy language; compiler; independent finite reference checker; counterexamples; representation migration certificates |
| `research.py` | Bounded repair search; learned frozen search policy; reusable repair library; factorial transfer experiment; human-designed baseline; conservative uncertainty; artifact export |
| `ledger.py` | SQLite tasks; leases and fences; idempotency; budget reservations; uncertain charges; reconciliation; atomic accepted results |
| `artifacts.py` | Atomic SHA-256 object store and verified reads |
| `graph.py` | Versioned dependencies; cache invalidation; branch-safe snapshots; read/write conflict checking |
| `routing.py` | Verified-capability routing plan; normalized allowance balancing; separate API budgets; connection reserve |
| `promotion.py` | Evidence threshold; exact-artifact approval binding; activation history and rollback |
| `cli.py` | Integrated local run, evidence export, accounting and proposal recording |

See [validation](docs/VALIDATION.md) for executed results, [primary sources](docs/RESEARCH_SOURCES.md) for the literature and provider corrections, and [deployment boundaries](docs/DEPLOYMENT.md) for the two-VPS integration contract.

## What the experiment means

Each independent lineage develops a search method on development tasks, freezes it, and tests four method/library combinations on fresh tasks. A fixed all-guards-first comparator is also measured. Candidate slots are equal within final evaluation; total research expense is reported separately and is not matched between learned and baseline arms.

The current domain contains 32 guard combinations. A known complete guard set solves it; therefore success demonstrates an auditable learning-and-evaluation pipeline, not a frontier algorithmic advance. Fresh seeds remain instances of the same finite family. A complete-policy fallback is independently checked in the final candidate slot when a learned method has not found an incumbent. The pilot's statistical result remains separate from engineering release checks; inconclusive evidence cannot activate a release.

The exported Python is built from trusted templates. No arbitrary model-generated code or shell is executed. The system trains no neural model weights, makes no paid calls, and does not deploy itself. The controller and promotion registry assume trusted local callers.

## Next implementation boundary

To work on your actual codebase: connect the existing repository and VPS hosts, implement authenticated worker transport and approved provider invocation adapters, add repository-specific independent evaluators, and test isolated patches. The twenty-part document specifies how to extend the prototype toward that system and which claims would require new experiments.
