# Validation record

Release: 0.1.0 · Python 3.12.14 on the connected Linux workspace · 21 September 2026.

## Executed checks

- `python3 scripts/run_checks.py` — **100 tests passed in 0.917s**, followed by a complete temporary-directory demo and routing inspection. Full output: `examples/final-checks.txt`.
- `python3 scripts/compare_revisions.py` — final fixed comparison **passed**; 263/384 → 384/384, zero losses. Full results: `examples/revision-validation/summary.json` and paired raw reports.
- `python3 -m ryan_frontier pilot --units 8 --budget 6 --output examples/final-run` — completed, zero provider calls, zero API cash charges, scientific promotion remains `inconclusive`.

## Ten release criteria

These are concrete engineering checks, not a general 10/10 quality rating.

| Criterion | Result | Evidence |
|---|---|---|
| All behavioral tests | Pass | 100 tests across six suites |
| Crash/retry/budget correctness | Pass | 22 ledger tests including real process exit and concurrent races |
| Bounded safety and nonvacuity | Pass | 19 domain tests; zero-policy-coverage acceptance denied |
| Generated method behavior | Pass | 27 research tests; exported Python drives verifier and matches policy semantics |
| Budgeted fallback | Pass | Budgets 1, 2, 3, 6, 32; no free/duplicate fallback check |
| Version and artifact integrity | Pass | 19 infrastructure tests including divergent branches and corrupted objects |
| Exact evidence release gate | Pass | Six promotion tests and seven integration tests |
| Fresh revision comparison | Pass | 64 lineages, 384 cases, zero losses or original-control changes |
| Honest baseline and scientific status | Pass | Fixed baseline retained; inconclusive evidence unpromoted |
| Codex handoff and reproducible entry point | Pass | START_HERE, AGENTS, CODEX_PROMPT, run_checks, raw evidence and package manifest |

## Reproduction and limits

From the extracted project directory on Linux/POSIX or WSL2:

```bash
python3 scripts/verify_package.py
python3 scripts/run_checks.py
python3 scripts/compare_revisions.py
```

The last command rewrites deterministic comparison reports using the recorded protocol. Do not call repeated runs independent confirmation. Code changes informed by these results require a new recorded final-validation protocol and fresh data.

Generated workflow programs are drawn from a 32-policy closed grammar. The finite checker uses explicit event horizons and bounded workers/fences. The final fallback is a human-known solution in this domain. No test here establishes unrestricted code safety, remote authentication, provider integration, equal-total-cost superiority, or frontier-level general intelligence.

Exact recorded run identities:

- Pipeline: `f20c6486a830a878602dc4f7c3698d3405c35eeafe2fb8f8c56116e2221d45a3`
- Evaluator: `de98009d8f64cb43370e223e9c8d79ba81f9c0e81bdb6508c38dfcacbd748ee1`
- Candidate bundle: `1d6849c7c0182fc2978f2012229dc9f635375a572d39a9f8b4e22db974aaf2df`
- Report: `f8ad5b6c40abe12740393ba994c96820f3901bd5c83fa2b7b1eae878e0518085`
- Receipt: `5706f2f7d85015ae1d756b399ea017ea5854b578afea1bc56bcf0fc2af147a7b`

The package-level `MANIFEST.sha256.json` covers source, documentation, tests, and packaged evidence. Hashes identify bytes; they do not prove scientific correctness or authorship.
