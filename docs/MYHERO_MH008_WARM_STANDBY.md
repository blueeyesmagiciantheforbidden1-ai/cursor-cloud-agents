# MH-008 warm standby: design options (not implemented)

Status: proposal for review (Light) and an operator decision. Nothing here is built or deployed.

## The measured problem

Retina measured this on 2026-09-25 over 100 executions (`Desktop\Retina\mh007b\mh007b-executions.csv`):
- create→Started has a median of 156 s (58-341 s). About 95% of that is Cloud Run instance provisioning after the image is ready.
- Image import is about 1 s, so a smaller image would not help.
- There is no sign of quota pressure: the delay does not grow when starts overlap.

## Why the delay is on the user's path

The design is serial and deliberate (`live_loop.py` line 1: "One warmed native process, one claimed user task, then cleanly drain"; `fleet_controller.py` `tick`):

1. The worker for slot X claims one step, answers it, releases its credential and exits.
2. The controller launches the next execution only after the previous one is **terminal** and the credential is **cleanly released** (`current_terminal` → `prior_execution_still_active`, `idle_credential`).
3. The new execution then spends about 156 s in provisioning, plus its provider `prepare()`, before it can claim.

So agent X has no ready worker for about 3 minutes after each of its turns. In a five-agent round-robin room, X's next turn is four steps away, which usually hides the gap. It does not hide it when:
- the same agent has back-to-back work: single-agent rooms, a repair attempt, a retry, or two rooms in a row;
- the probe or first step of a new room lands inside that gap. This fits the baseline's 540 s and 708 s claim waits.

The serial rule exists for safety. There is one credential lease per account, no parallel holders, strikes on unclean exits, and no paid restart loops. Any option must keep all of that.

## Options

### A. Reuse the warm container for a second turn (worker change)

After completing a task, the worker re-prepares a fresh native session in the same execution and claims again, until its warm window ends. The container stays provisioned, so the 156 s goes away and only `prepare()` remains.

- Keeps one execution and one credential holder at a time. The controller is unchanged.
- It changes the invariant "one claimed task per execution". Every provider adapter assumes a single-use native session. Credential writeback has to commit between turns, and a second acquire must be checked against the broker's fence and version (does the broker allow the same execution to re-acquire?).
- The failure blast radius grows. A bad second turn happens inside the same execution, and the strike accounting has to count turns, not executions.
- Effort: large, and it touches all five providers plus the broker. Highest risk to the parts that failed in the 09-23 incidents.

### B. Pre-launch the successor while the active worker runs (controller + worker change)

When the active execution claims a task, the controller launches the next one in a new `standby` phase. The standby provisions during the task, then waits for the credential and acquires it only after the active one has cleanly released it.

- The provisioning delay is overlapped instead of removed. The standby still does `prepare()` after the release.
- There are two executions per slot for a short window, so the controller's single-execution state machine needs a second tracked execution:
  - its own intent and grant;
  - never binding while the predecessor holds the credential;
  - a clean path when the active execution fails (does the standby become active, or is it cancelled?).
- It needs a trigger. The controller does not read the hub today. The worker's `busy` report or the hub's attempt record (`claimed_at`) would have to be read, or the worker could signal the controller.
- Cost: one extra idle instance per slot for about the length of one task.
- Effort: medium-large, concentrated in `fleet_controller.py`, the area with the most careful proofs (re-key, archive receipts, never_bound).

### C. Launch the replacement earlier in idle time (controller-only, smaller)

The biggest measured waits were first steps and probes, not back-to-back turns. Today a worker that ends its warm window idle exits, and only then does the controller relaunch it, so every idle hour ends with a 3-minute gap. Two narrower changes:
1. **Staggered warm windows.** For an idle worker, choose its warm deadline so the slot's replacement is already running when the old one drains. That is a variant of B that applies only to idle drains, where no credential is in use for a task. The old worker is idle, so its drain-and-release is short and predictable.
2. **Report the gap honestly.** The hub's `hub_status` already shows per-agent readiness. Make `ready_for_project_prompt=false` during a relaunch visible to the manager, so dispatch can prefer another agent in flexible rooms (MH-008 fairness) instead of waiting.

- It doesn't help back-to-back turns for the same agent.
- Effort: small to medium. C2 is hub-side only and low risk.

## Recommendation

1. Do **C2** first. It's hub-side and low risk: dispatch stops waiting blindly on an agent that is provisioning.
2. Measure again with P0.1's durable `attempt_records[].queued_at`, which splits queue wait into "no ready worker" and "worker busy elsewhere". Use Retina's CSV method for the start phase.
3. Then choose between **B**, which overlaps provisioning and keeps one credential holder, and **A**, which removes it but changes the one-task invariant, based on how much of the measured wait is back-to-back same-agent work.

Either B or A needs Light's review of the credential-exclusivity argument before any code.

## Acceptance for whichever is built (from the release contract)

- Runnable-to-claim with a warm ready worker: p95 ≤ 15 s, p99 ≤ 60 s under declared normal load.
- Never two credential holders for one account: a test and a broker-side assertion.
- A failed active or standby worker never becomes a paid restart loop: the existing strike and backoff rules still hold.
- The rollback is one configuration flag that returns the slot to today's serial behaviour.
