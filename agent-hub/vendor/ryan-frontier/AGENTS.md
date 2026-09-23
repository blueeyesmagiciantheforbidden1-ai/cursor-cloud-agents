# Codex handoff rules

Read `START_HERE.md`, `docs/VALIDATION.md`, `docs/FRONTIER_20.md`, and `docs/DEPLOYMENT.md` before extending this project.

- This is a Python 3.11+ Linux/POSIX research prototype, not a deployed command center. Runtime dependencies are standard-library only.
- Keep existing source, evidence, and user files intact. Keep historical revision evidence immutable; use a new directory for subsequent experiments.
- Run `python3 scripts/run_checks.py` before reporting a change complete. Add tests only for real changed behavior or a concrete risk.
- The user prefers the strongest available approved model and highest supported effort. Verify exact model/effort capabilities; do not invent IDs or silently downgrade. The available provider apps and VPS hosts have not been connected by this package.
- Do not print secret contents. A Desktop/API_KEYS folder, if later actually available, is not evidence of permission to send every key to every provider. Use only the credential relevant to the authorized operation.
- Keep subscription-native integrations separate from metered APIs. Respect actual supported integration and account permissions. A routing plan is not a provider invocation or a quota reservation.
- Keep evaluator, account budget, approval authority, and protected confirmation data outside candidate write access. The current closed DSL is not a sandbox for arbitrary generated code.
- Preserve exact candidate, evaluator, and evidence identities through release. Inconclusive evidence cannot be converted into success by relabeling a result or rerunning until a favorable seed occurs.
- Include failed candidates, development, verification and migration costs. Independent lineages are the statistical units; multiple tasks from one descendant are not independent lineages.
- State observed improvements and remaining limitations precisely. Do not rate the project 10/10 or claim frontier superiority without a defined benchmark and supporting evidence.
- Carry out authorized local and reversible implementation work. Production changes require the task's actual authorization and a connected target; do not pretend this archive has deployed anything.
