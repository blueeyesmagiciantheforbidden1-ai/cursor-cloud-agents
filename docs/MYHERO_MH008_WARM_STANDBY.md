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

## Review (Light, 36f8b40): one gap cannot explain 540 s or 708 s

The worst single relaunch gap is about 420-460 s, and typically about 250 s:
- the release,
- plus one controller tick (up to about 60 s),
- plus provisioning (at most 341 s observed),
- plus `prepare()`,
- plus the first poll.

708 s is past any observed gap, and 540 s is past the 99th percentile. So the baseline waits need another component. Candidates from `fleet_controller.py`:
- **a. Failure backoff** `FAILURE_BACKOFF_SECONDS = (120, 600)`. A strike on the previous execution adds 120 s, then 600 s. 120 s + about 340 s + prepare ≈ 540 s, and the 600 s tier alone nearly gives 708 s. The 25c copilot blocker (a strike on a healthy agent) was exactly this kind of event.
- **b. Quota park** (at least 3600 s). Too long for these numbers, but it must be excluded explicitly.
- **c. Busy elsewhere.** The slot is serving another room for up to `timeout_seconds`. That is not a relaunch gap at all.
- **d. Two gaps in series**, for example an idle drain just before the probe followed by a prepare failure and a relaunch.

(a) and (c) look like provisioning from the manager's side, but neither B nor C1 fixes them.

## Baseline data (timing_details.json, campaign mh-extreme-f11b93494a, 2026-09-24 UTC)

| Agent | Room / step | Claimed (UTC) | Wait |
| --- | --- | --- | --- |
| claude | claude lane 215fbb4b… step 1 | 00:26:45.75 | 539 s |
| copilot | chain c983bafd… step 1 | 00:26:48.28 | 545 s |
| cursor | cursor lane a368133b… step 1 | 00:26:54.53 | 560 s |
| copilot | copilot lane c60524f6… step 1 | 00:30:56.05 | 663 s |
| cursor | cursor lane step 2 | 00:35:53.48 | 523 s |
| claude | probe 176f3a0d… step 0 | 00:39:33.98 | 708 s |

Three different agents claimed within 9 s of each other after waits of about 9 minutes. This points to a **fleet-wide** event more than per-agent backoff, which fits Retina's MH-007 note that pending tasks from all five jobs were released in the same second. The remaining causes to separate are:
- a controller tick delay;
- a synchronized relaunch after all the step-0 executions ended around 00:17-00:18;
- a project-level Cloud Run hold.

Six rooms (the chain and five lanes) queued work on one slot per agent. So the later waits (copilot 663 s, cursor step 2, the probe) also include busy-elsewhere time and serial relaunch gaps. Retina is attributing these with the fleet data. If the fleet-wide cause is confirmed, neither A nor B addresses it, and the fix sits in the controller's tick cadence or at the Cloud Run project level.

## Recommendation (revised)

1. **Attribute the two baseline waits first.** Join the baseline rooms' attempt timing with that agent's controller slot history over the window:
   - phase transitions;
   - `consecutive_failures` and `next_launch_at`;
   - the previous execution's exit code;
   - execution create, start and terminal times from Retina's CSV.

   This needs Retina's read-only access (Firestore `runcrew_fleet_state` history and the archived receipts, plus the execution list). From P0.1 on, `attempt_records[].queued_at` makes this routine.
2. **C2 with a reason, not a boolean.** Expose why an agent is not ready: `provisioning`, `backoff`, `parked`, `busy` or `offline`, so flexible dispatch treats a 600 s backoff differently from a 150 s provisioning.
   - The hub can already derive `busy`: the agent holds a lease in some room.
   - It can also derive `offline`/`stale` from the worker reports.
   - `provisioning`, `backoff` and `parked` are controller state. The controller **publishes** a small per-slot status document (`phase`, `next_launch_at`, a fixed reason code, `published_at`), and the hub only reads it.
   - The hub gets no access to `runcrew_fleet_state` or the receipts, which keeps its service account away from controller state.
   - The document is advisory and can go stale. The hub shows a reason only when `published_at` is newer than about two controller ticks, and otherwise shows `unknown`.
   - It feeds the readiness reason only. It never feeds a dispatch, lease or claim decision.
3. **C1 before B.** A staggered idle drain is the same exclusivity problem as B, in the easy idle-only case. Build the exclusivity mechanism there, and B inherits it.
4. **B's exclusivity proof, for Light's review before any code:**
   - the **broker** (fence and version on acquire) enforces a single holder, so a standby is refused even if the controller's view lags;
   - the standby acquires strictly after the release is committed, and a crashed active worker (no clean release) never lets the standby acquire; that case goes through the existing quarantine and reset path;
   - cancelling a standby costs nothing and gives no strike.
5. **A only if** the attribution shows that back-to-back same-agent work dominates. A changes the one-task-per-execution invariant that the 09-23 incidents relied on.

## Acceptance for whichever is built (from the release contract)

- Runnable-to-claim with a warm ready worker: p95 ≤ 15 s, p99 ≤ 60 s under declared normal load.
- Claim wait is reported as a separate p95 for each cause: no ready worker (provisioning), backoff, parked and busy elsewhere. A strike storm must not hide inside a provisioning percentile.
- Never two credential holders for one account: a test and a broker-side assertion.
- A failed active or standby worker never becomes a paid restart loop: the existing strike and backoff rules still hold.
- The rollback is one configuration flag that returns the slot to today's serial behaviour.
