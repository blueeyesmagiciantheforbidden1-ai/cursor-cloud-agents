# Start here: Codex-ready implementation

This package expands the original five-part self-improving-code proposal into twenty parts, with runnable research infrastructure and recorded improvement evidence.

## Use it in Codex

1. Extract the archive and open the `ryan-frontier` directory in Codex.
2. Ask Codex to read `CODEX_PROMPT.md` and follow it. It points to the architecture, current evidence, and next engineering milestone.
3. On Linux/POSIX or WSL2 with Python 3.11+, run:

```bash
python3 scripts/verify_package.py
python3 scripts/run_checks.py
```

Neither command calls a model provider or spends API credits. The check script uses a temporary run directory.

## Read these first

| File | Purpose |
|---|---|
| `docs/IMPROVEMENT_REPORT.md` | Original weakness, corrective changes, fresh-data comparison, and remaining limits |
| `docs/VALIDATION.md` | Exact release test results and runnable verification commands |
| `docs/FRONTIER_20.md` | Twenty-part research, mathematics, and implementation architecture |
| `docs/DEPLOYMENT.md` | Actual two-VPS integration boundary and provider contracts |
| `docs/RESEARCH_SOURCES.md` | Checked primary literature and corrections to old provider advice |
| `CODEX_PROMPT.md` | Ready-to-use continuation task |
| `AGENTS.md` | Repository working instructions |

## Current state

The working core generates bounded workflow-repair programs, independently checks them, learns search policies from development feedback, exports executable methods, and measures transfer. A durable controller ledger, artifact store, branch-safe dependency graph, provider-routing planner, and release registry support that core.

The revision loop found and fixed an actual negative interaction: repair memory could consume candidate slots before the learned method found a valid solution. Revised learned methods admit memory after first success and independently check the known complete policy as a final fallback if no repair has passed. Both steps stay inside the declared candidate budget. The package retains the failed intermediate revision that motivated this fallback. This is a manually engineered correction to the research machinery; the search ordering itself is learned from development experiments. The package distinguishes those facts.

The known all-guards repair remains a perfect baseline in the small domain. Improvements here do not establish frontier superiority, arbitrary-code self-improvement, or statistically proven general transfer. See the measured report rather than treating “20” or “10/10” as a certified scientific score.

No existing repository, desktop, VPS, Alibaba account, or provider app was connected during this build. The next production step is to establish those connections and implement authenticated worker transport and a real repository evaluator. The package provides that continuation specification without pretending it is deployed.
