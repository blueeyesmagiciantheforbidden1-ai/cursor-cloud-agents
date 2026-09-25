# MyHero usefulness benchmark (MH-014)

Held-out coding tasks to compare three ways of working on the same task (guide item 11):
- A: one strong approved coding agent;
- B: targeted implementation plus independent review;
- C: a multi-agent workflow.

The acceptance tests are the same in all three and the agents never see them.

## Task layout

```
bench/tasks/<NNN-slug>/
  meta.json          # see below
  TASK.md            # the request, written as a user would write it (no hints about hidden tests)
  repo/              # starting code, a small self-contained Python project (stdlib only)
    <package files>
    tests/           # VISIBLE tests the agent may read and run (may be incomplete)
  hidden_tests/      # TRUSTED acceptance tests, never shown to the agent (test_*.py)
  reference/         # full-file replacements that solve the task (paths relative to repo/)
```

`meta.json` fields:
- `id`: "NNN-slug"
- `category`: bugfix | feature | refactor | test-writing | review | multi-file
- `difficulty`: 1-5
- `time_budget_s`
- `expected_files`: the files a correct solution changes
- `multi_agent_suitable`: true | false
- `notes`

## Rules for authors

- Python standard library only; no network; each task runs in under 10 s.
- The hidden tests must FAIL on the starting repo and PASS after the reference files are copied over it. The visible tests must pass after the reference as well.
- The hidden tests check behaviour, not implementation details. They should catch plausible wrong solutions: off-by-one errors, missing edge cases, mutation of inputs, wrong error types.
- TASK.md must be answerable from TASK.md plus repo/. Never mention hidden tests.
- No secrets, real names, emails, hosts or URLs.
- Run `python bench/validate_tasks.py bench/tasks/<id>` for every task you add. It must print OK.
