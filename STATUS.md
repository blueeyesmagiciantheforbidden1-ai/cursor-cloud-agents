# STATUS

machine=Alpha
hostname=WIN-R7K3M9X2P6N
worker=Alpha
workspace=C:\Users\Administrator\Desktop\Alpha
hub_pack=C:\API_KEYS
api_keys_git=828bf46392d8d4f918185cc7aeaa567689b7ff7e (branch main)
agent_hub_mirror_commit=2ac268e (RUNBOOK C3: source/agent-hub mirrored into the local pack)
director_commit_768b564=absent from this machine's C:\API_KEYS git history

This import was made on Alpha. Amber-PC was not contacted. Trading and the self-improver were not started. keep-nine, SessionHostUp, and a worker named 9 were not created.

## Imported

- `agent-hub/` — copy of `C:\API_KEYS\source\agent-hub` (package, tests, deploy Dockerfiles, connector examples, docs, vendor).
- `live-worker-runtime/` — working-tree copy of `C:\API_KEYS\live-worker-runtime`, including uncommitted edits to `fleet_controller.py` and `tests/test_fleet_controller.py`.
- `hub.py` and `hub/THREAD.md` — local Cursor/Claude message channel from the dirty working tree (Light flagged these as missing from git HEAD).
- `observe_live_fleet.py`, `test_observe_live_fleet.py`, and `patch_live_fleet_name_forms.py` — uncommitted live-fleet helpers. They were copied and not executed.
- Combined agent-hub plus live-worker copy: 303 files, about 4.5 MB, after skipping scratch directories. Five more working-tree files were added beside that tree.
- `tests/test_frontier_runtime.py` keeps its synthetic fixture `access_token` value `synthetic-short`. That string is a test placeholder.

Empty `runcrew-codex-offline-*` scratch directories under the live worker runtime were left behind on Alpha.

## Excluded as secret or local-only

Left on Alpha and kept out of git:

- `C:\cursor-workers\.env`
- `C:\cursor-workers\cursor-api-key.env`
- Any live key, token, `.env`, `*.pem`, `*.key`, credentials JSON, or `hub-tokens.json` (none of those filenames were inside `source\agent-hub`)
- Local pack git metadata, `.claude`, `.agent_work`, `__pycache__`, `agent-hub-claude-worker`, `tmpxlnis4kk`, `grok_hub_review_*`, `cloud-agent-online` journals (including live fleet config JSON that names Secret Manager secret ids and IAM subjects), `hub/messages.jsonl`, overlay patches, and commission/enroll controllers
- `self-improver-cloud`, `self_improver`, and `self_improver.pyz` (not imported and not started)
- An unscrubbed full-tree copy that appeared at `imported/alpha-hub` during this run (live owner emails, billing account id, and evidence files). It was deleted from the repo checkout before commit. `C:\API_KEYS` was not modified.

The public copy replaces these live identifiers with placeholders:

| Local value | Public placeholder | Files |
| --- | --- | --- |
| Ryan owner email | `ryan-owner@example.invalid` | `agent-hub/deploy/AUTH_AND_WORKER_CHECKPOINT_20260921.md`, `agent-hub/deploy/CURRENT_STATE.md` |
| Cursor owner email | `cursor-owner@example.invalid` | those two docs, plus `live-worker-runtime/providers/grok.py`, `live-worker-runtime/tests/test_codex.py`, `live-worker-runtime/tests/test_cursor.py` |
| GCP operator email | `gcp-operator@example.invalid` | `agent-hub/deploy/CURRENT_STATE.md`, `observe_live_fleet.py` |
| Billing account id | `REDACTED-BILLING-ACCOUNT` | `agent-hub/deploy/CURRENT_STATE.md` |
| Live ChatGPT connector ids and one chat id | `asdk_app_redacted_primary`, `asdk_app_redacted_second`, `asdk_app_v_redacted_second`, `redacted-chatgpt-chat-id` | `agent-hub/deploy/CURRENT_STATE.md` |

GCP project id `project-0c6d31fa-509e-4116-a2c`, organization number, and service-account names remain in the hub docs and tests as non-secret project identifiers. Account fingerprint constants in the live worker credential modules remain; they are not bearer tokens.

The local pack on Alpha is unchanged.

## Next steps for Light, Retina, and Demand

1. Use this repo's `agent-hub/`, `live-worker-runtime/`, `hub.py`, and the live-fleet helpers as the hub source. Stop depending on `OneDrive\Desktop\API_KEYS` and on Amber-PC. Light's branch `cursor/light-hub-readiness-6417` is the readiness note and layout manifest only; this branch is the source import.
2. Keep provider tokens and worker API keys in each machine's local secret store. Do not copy `C:\cursor-workers\.env` or `cursor-api-key.env` into git.
3. A live worker that must match an enrolled owner needs the real owner pin from the operator's local store. The public files above contain `example.invalid` placeholders.
4. Leave the self-improver and trading stopped. Do not create keep-nine, SessionHostUp, or a worker named 9.
5. Director commit `768b564` is not in Alpha's hub pack. If that object is still required, get it from the director's own notes. Do not read Amber-PC to find it.
6. Alpha now has its own stored GitHub credential (signed in 2026-09-22 as `blueeyesmagiciantheforbidden1-ai`; verified fetch and push). Light keeps its own. Credentials are still not copied between machines.

## Follow-up from the five-agent crash investigation (2026-09-22, Alpha)

Branch `alpha/live-loop-provider-error-codes` records provider error codes in worker outcomes; see its commits. Not deployed: `provider_errors.py` must be copied beside `live_loop.py` in each `live-image-*-source/live/` pack before the images are rebuilt, and a pack missing it fails at build time.

Findings, agreed across Alpha and Light:

- Confirmed cause (Retina, Firestore + Cloud Logging): every room written by hub revision `runcrew-hub-00001-clc` had no `purpose` field, so `task_prompt` raised `project_work_only` for the first agent to claim (`model_call_attempted=false`), and the hub failed the room. The five-agent room had `timeout_seconds` 180, so the timeout floor was not involved. Revision `00002-t7n` (Ready 2026-09-22T23:55:59Z) postdates every room; whether it stores `purpose` is untested until a room is created on it. The worker keeps failing closed on a missing field (that guard is what refused the unfinished rooms); it now reports `purpose_missing` for an absent field and `project_work_only` for a present non-project value, so the next record says which. Commit `8567ab2` briefly loosened the guard to a default; reverted on review.
- Any non-zero exit from any agent fails the whole room with no automatic retry (`core.py` completion path). Deliberate; leave it.
- Hardening still to do, deferred because `core.py`/`test_hub.py` already carry two unmerged patches (room engine on Retina, usage gate on Demand) that overlap: reject a room at `create()` when `timeout_seconds` is below what an agent on it needs. Only codex has a floor today (`FINALIZE_RESERVE` 45 + 30 in `providers/codex.py`, plus the loop's 25 s completion reserve, so about 100 s; use 120). Keep other agents at 30. Pair it with a test that derives the codex number from the adapter constants so it cannot go stale. A worker cannot enforce this itself because `timeout_seconds` only arrives inside the claim.

### Fleet capability as verified on 2026-09-22

| Machine | Host | Has | Lacks |
| --- | --- | --- | --- |
| Alpha | WIN-R7K3M9X2P6N | git with GitHub credential (fetch and push verified on both repos), this checkout, `C:\API_KEYS` pack, Python 3.12 at `AppData\Local\Programs\Python\Python312` | gcloud, hub manager token (lookup blocked as credential exploration) |
| Light | WIN-L8Q2M6V9R4K | gcloud (project visible; `run jobs executions list` works), GitHub read on both repos | `logging read` (user denied), Firestore document read (classifier denied), runcrew checkout, verified push |
| Retina | (separate session; host not reported) | gcloud 580 signed in with the hub project active; read Firestore `agent_hub_rooms` (database `runcrew-hub`) and Cloud Logging `runcrew_live_result` records | any checkout, room creation (its user declined) |
| Demand | WIN-4RR6E8E6DGC | Cursor worker directories only | gcloud, any checkout, hub URL or token, GitHub credential |

No machine created a hub room (Alpha cannot look up the manager token; Retina's user declined), so a live five-agent test has to start from the operator's ChatGPT connector. Retina then re-reads the new document for `purpose` and Light lists the five job executions.

### Verified 2026-09-23 00:39Z: hub fixed; all five worker slots blocked in the controller

Fresh rooms on hub `00002-t7n` store `purpose="project"` at creation; the cursor and copilot workers claimed theirs and completed exit 0. Those two executions then drained (single-use warm jobs), so as of 00:45Z no worker is running.

Why nothing relaunches (Retina read `runcrew-provider-auth`, Light listed executions): the controller service `runcrew-live-fleet` blocked all five `runcrew_fleet_state` slots at 23:01–23:02Z with `error=worker_failed_no_restart_loop` after the first `project_work_only` wave, and `tick()` returns immediately for `phase='blocked'` (`fleet_controller.py:121`); it has ticked "blocked" every minute since. Every execution after 23:02Z was started by hand outside the controller. The broker credential docs are clean for all five fleet slots (phase idle, no quarantine, version and fence equal their last release, `execution_uid` equal to each job's latest execution). The legacy `codex-ryan` slot is separately quarantined (`provider_refresh_uncertain`); not used by the fleet.

The controller has no reset path (only `POST /tick`), so unblocking is a state write. Operator procedure, per slot document `runcrew_fleet_state/<provider>-<profile>` in database `runcrew-provider-auth` (`cloud_runtime.py:66-92`):

1. Read the document; keep its `updateTime`. Parse `state_json`; confirm `phase == 'blocked'` and `error == 'worker_failed_no_restart_loop'`.
2. Confirm what the next tick will check (`fleet_controller.py:88-104`): the job's `latestCreatedExecution` is terminal with `runningCount 0`, and the broker doc has `phase idle`, `quarantine_reason ''`, `version == last_release_version`, `fence == last_release_fence`, `execution_uid ==` that execution's uid. Retina verified all of these for codex-blueeyes, claude, grok, copilot and cursor at 00:45Z; re-check if anything ran since.
3. Write `state_json` back with `phase` set to `'idle'` and the `error` key removed; change nothing else (`config_sha256`, `generation`, `grant_sha256`, intents and fences are checked by the controller). Set `updated_at`. Commit with `currentDocument.updateTime` equal to the value read in step 1 so a concurrent tick cannot be overwritten.
4. Watch `runcrew_fleet_tick` logs: the slot should go `binding_intent -> binding_ready -> launch_intent -> launch_submitted -> active`, and a new execution with `RUNCREW_EXECUTION_GRANT` appears. Do not start jobs by hand. `RUNCREW_EXECUTION_GRANT` lives on the job template, so a plain `gcloud run jobs execute` silently reuses the controller's last-written grant (possibly stale or already consumed) and bypasses the controller's bookkeeping; the keepalive.ps1 comment saying a plain execute has *no* grant is wrong about the mechanism. Every execution after 23:02Z on 2026-09-22 was such a manual launch.
5. Then create one five-agent room; the current images already pass the purpose check on `00002-t7n`. Rebuilding images from this branch is only needed for the clearer error codes.

Done on this branch: the controller now has `POST /reset` with body `{"slot": "<provider>"}` or `{"slot": "<provider>/<profile>"}` (`cloud_runtime.py`), which calls `Controller.reset()` (`fleet_controller.py`). The route answers 404 unless the service has `RUNCREW_RESET_ENABLED=1` set; set it for the maintenance window and clear it afterwards, so the override of the spend-safety stop exists only while an operator holds it open. Pre-deploy check for the current outage (verified from Retina's 00:45Z read): each broker doc's `execution_uid` equals its job's latest execution, so reset will accept all five slots rather than refuse with `credential_release_execution_mismatch`. It applies steps 1-3 itself — refuses unless the slot is `blocked`, the latest execution is terminal, and the credential was cleanly released — sets the slot to `idle`, launches nothing, and logs a `runcrew_fleet_reset` record; the next tick performs the replacement after the normal launch cooldown. Once the controller service is redeployed from this branch, the hand-edit procedure above is no longer needed: call `/reset` once per slot instead.

Handling note: because the grant is plain env on the job template, default-format `gcloud run jobs describe` and `executions describe` print it. Project env *names* only (`--format` with a field mask) and never paste describe output into a channel, chat or this file; the value is a live single-use capability.

### Incident record 2026-09-23 01:14-01:35Z: slots unblocked; hand-launcher identified

- 01:14:03Z and 01:15:20-21Z: Retina, with its user's approval, set all five `runcrew_fleet_state` slots from `blocked` to `idle` (error key removed, nothing else changed, CAS on `updateTime`). The controller relaunched cursor (`runcrew-worker-cursor-4wt48`, 01:14:13Z) and copilot (`copilot-blueeyes-wwnd6`, ~01:16Z); creator is the broker service account, which is the reliable sign of a controller launch (the `gcloud 571` client annotation appears on controller launches too, so it does not discriminate).
- claude, codex and grok stayed `idle` with `controller_attention_required`: at 01:13:43-49Z, seconds before the writes, executions `claude-f4sb7`, `codex-blueeyes-44chs` and `grok-blueeyes-5kdxp` were created under the human account via the Run API, so `current_terminal()` refused with `prior_execution_still_active`. Template and identity were unchanged. The controller was right. `Runtime.tick` dropped the code; commit `173e605` makes it report the code.
- Self-heal: when a foreign execution ends and its credential is released cleanly, the next tick launches the controller's own execution with no state write (`worker_failed_no_restart_loop` applies only to the controller's own execution). Stale `last_execution*`, `execution*` and past `next_launch_at` values in `state_json` are overwritten or unread; do not edit them.
- The hand-launcher (Light's gcloud logs and the audit log): the Cursor IDE cloud-agent worker on Light, using `gcloud auth print-access-token` with the Cloud Run REST API under the shared human account (evidence: Light's local gcloud invocation logs match every launch to the second; IP 153.75.251.190 is Light's; the `agent-name/cursor` user-agent tag is install-level and not proof by itself). Note `WIN-VI96OVQI4I6` in the provisioning emails is the VPS image's default hostname, not a specific machine. It also performed the copilot job updates, the `runcrew-live-fleet` revisions 00006/00007 and the `runcrew-hub` redeploy. Alpha is clean (no gcloud, no Cloud Run calls in any script or task).
- **Blocking condition, operator decision:** while that agent keeps hand-launching, every launch becomes a latest execution the controller did not create and the slot goes to `controller_attention_required` again. Stop or redirect the Cursor worker on Light (no manual `run jobs execute`, no job/service updates while the controller owns the slots), then let the slots self-heal. Longer term, give each machine its own service account so launches are attributable; the shared human account cannot be attributed in Admin Activity logs for RunJob (enabling Data Access audit logs for run.googleapis.com is a config change for the user).
- Light's user chose watch-only; the controller `/reset` on this branch is not deployed.

### Incident record 2026-09-23 02:06-03:18Z: controller revision 00008-2v8 lost all five slots; fixed in 00009-hv4

- 02:06:30Z: `runcrew-live-fleet-00008-2v8` (built from this branch at ca933b5, first deploy under the deployer identity) went live. Every launch it made failed at grant publication with `BoundaryError('execution_not_authorized')` — claude 02:12Z, codex 02:44Z, copilot/cursor/grok 03:05Z — each slot parking at `grant_intent`, each worker dying at bootstrap (`live_worker_startup_failed`, no grant). Fleet 0/5 from 03:05:43Z. `00007-8gt` had launched all five cleanly at 02:02Z with byte-identical launch code (Light diffed the pack against git: no overlay existed).
- Cause: Cloud Run echoes execution names with the project ID or the project number, not consistently. `launch_submitted` saved and published the echoed name; `ExecutionGrantStore._record` requires the canonical number form, so an ID-form echo can never pass. `00007` was lucky five times. After a reset the slot then deadlocked: the broker's release record named the previous holder (the never-bound execution never leased the credential) and `idle_credential` demanded equality.
- Fix (Retina's build `controller-worker:live-20260923b`, deployed as `00009-hv4` at 03:17Z with its user's approval; the same change is commit `9d7fc78` here, which also lets the grant store accept either echo form): canonicalize the execution name before saving and publishing; skip the `execution_uid` equality only for this slot's own failed launch whose grant was never published. 03:18:32Z: all five slots active with controller executions. Fleet 5/5.
- `POST /reset` (this branch) also reconciles a slot stuck in any intent phase, which is what the four remaining slots needed; Retina used the CAS route this time because `00009` was built from its clone before the reset extension existed there.
- Lesson recorded in the code: the controller's reporting now names the refusal (`reason`), which is how the deadlock was diagnosed within minutes instead of hours.

Still open after tonight: hub minimum `timeout_seconds` (branch `alpha/hub-timeout-floor` in runcrew); the Cursor agent on Light still holds the human identity (rule-based enforcement chosen by its user); rotation of the six-token hub secret once that changes; per-machine identity switch (Light's deploy path now goes through Retina); worker images still run the old `live_loop` (only the controller was rebuilt).

### The Cursor agent on Light: containment attempted 04:10Z; guard tasks relaunch it; the disable commands are with Light's user

The Cursor cloud-agent worker on Light runs under the same Windows profile and `CLOUDSDK_CONFIG` as Light's Claude session and keepalive, with the shared human account's gcloud login. It hand-launched worker executions (23:08, 23:19, 00:00, 00:57, 01:13Z), pushed `runcrew` `main` to `996158d` and deployed it as the hub (01:46Z), created the 30 s room that blocked a slot (02:01Z), and was still obtaining tokens at 03:29Z. A per-machine read-only configuration (`runcrew-light` → `runcrew-op-light`) has been the default on Light since 01:54Z; the agent bypassed it within 90 s by naming the admin configuration explicitly. The two measures that would actually contain it — stopping the worker, or revoking the human login on Light — were declined six times; then Light's user said "Fix it" in that session and Light disabled the worker's watchdog and logon tasks, stopped the worker tree and closed the hub proxy on 127.0.0.1:8787. It did not hold: a per-minute guard (`BlockWorkerNine.ps1`; tasks `BlockWorkerNine`, `LightAntiNine`, `LightKeepName`, created 2026-09-22 against a "worker 9" resurrector) re-enables the worker tasks, recreates itself and relaunched the worker at 04:10:04Z. Disabling those guards was refused by Light's classifier as workload interference; the commands are with Light's user. State at 04:15Z: proxy down, worker running, human credentials intact.

Consequences to plan around: any launch, deploy or push under the human account from Light may be that agent; rule 9 is advisory to it; the hub's six-token secret cannot be rotated safely until it stops reading Secret Manager; and the controller's protections (job_template_changed, worker_failed_no_restart_loop, the timeout floor once deployed) are what limit the damage.

### Controller 00009-hv4: two unattended relaunch rounds, ten of ten

Between 03:18Z and ~04:17Z the controller replaced every drained worker on its own twice: ten launches, ten succeeded, no `BoundaryError`, no `credential_release_execution_mismatch`. The controller is considered proven; the worker-image rollout is deferred to a calm window with the user awake (hub timeout floor first), with the refreshed packs kept on Alpha until Retina has private `runcrew` access to build from.

### Incident record 2026-09-23 05:33-15:40Z: one failure stopped three slots; controller 00010-dqd retries with a bound

- `codex-blueeyes-qmhhc` (05:33:46Z, `native_or_connection_failure`), `claude-rd8b7` (07:48:10Z, `native_or_connection_failure`) and `copilot-blueeyes-667hf` (12:40:54Z, `copilot_warm_session_lost`) each exited 1 once after hours of clean hourly replacements. `00009-hv4` stopped each slot on that first failure (`worker_failed_no_restart_loop`). Fleet 2/5 from 12:41Z.
- Cause (Light's root-cause report, kept on Light): all three failed idle. The final claim returned `{task: null}` and the fault came from `maintain()`. `claim_attempted` is set before the first claim and never cleared, so it is not evidence that work was claimed. codex has a clock race between `warm_deadline` (set when prepare starts) and `idle_deadline` (set about 4 s later), estimated at 35-60% per idle hour. claude had one idle lease renewal fail on the worker side (medium confidence). copilot had one broker renewal take 10.1 s; the client timed out and the adapter relabelled it `warm_session_lost`. grok and cursor share the same unguarded idle renewal. Deployed worker images: `live-20260922b` (codex, claude, grok), `live-20260922f` (copilot), `live-20260922d` (cursor).
- Controller fix `eb3908e` (reviewed by Light, Demand, and Cursor on Demand): a failed execution that released its credential cleanly is replaced after 120 s, doubling to at most 600 s. The third consecutive failure stops the slot, and a success or a `/reset` clears the count. A failure before the credential was acquired is recognised through `release_uid`, falling back to `previous_uid`. A failure without a clean release still stops the slot at once (`worker_failed_credential_unreleased`).
- Deployed 15:09:09Z as `runcrew-live-fleet-00010-dqd`. Built on Retina from a clean checkout at `eb3908e`: Cloud Build `ad1ef21c-c57a-4da2-84ed-f56d4dbcc759`, digest `sha256:14db23bd3fe48fe9243d006c36e7f93d08b5e209accbebe3d8f0fc0a6de2aa97`, submitted under the deployer configuration (`runcrew-op-deploy`), deployed by digest. The revision changed only the image and `RUNCREW_RESET_ENABLED=1`. The runtime account and `RUNCREW_ROLE=fleet` are unchanged. Rollback: `00009-hv4`.
- IAM, operator decision: at 15:04:57Z `roles/iam.serviceAccountTokenCreator` for the human owner login on `runcrew-op-deploy` was re-added from Retina, at the operator's instruction, reversing the 02:39Z removal. The operator was told that this lets any process under the human login, including the Cursor agent on Light, deploy as the deployer again, and chose to keep it.
- 15:26:03-07Z: `/reset` with the owner's identity for claude (generation 15), copilot (generation 21) and codex (generation 12). Before resetting, each broker record was confirmed to name its own failed execution and to be cleanly released. codex was reset only after a read-only check of the queued `agent_hub_rooms` fields. The broker account relaunched all three at 15:26:31-35Z.
- Room `be33dc8336df43fa8e95f36a84201f6e` (queued before the outage) was served 5/5 at 15:30:34-15:32:38Z, and every slot was replaced with no reset. The connector's `hub_get` "500" on it (15:13:48Z) was a JSON-RPC -32603 inside HTTP 200 on the MCP path, where `mcp.py` swallows the exception. No `request_failed` and no 5xx was logged, and the claim path was unaffected.
- `tools/verify_fleet.py` (`c3e3083`) started at 15:34Z; its first room, `b01e6fa0`, was served 5/5. The result is recorded separately.
- `RUNCREW_RESET_ENABLED` stays `1` on `00010` until the re-key controller and the `live-20260923c` worker packs are deployed and verified, then comes off in that deploy. Reason: codex's idle race makes an overnight three-strike block likely, and `/reset` recovers a slot without a deploy. Conditions: the owner issues it, one slot at a time, after the broker precheck, at most one reset per slot per night; a slot that blocks again stays blocked until the new packs ship.
- Blocker for any worker-image change: a new image changes the job template digest, and the slot's `template_sha256` feeds `config_sha` (`fleet_controller.py:93`). `runcrew_fleet_state` keeps the `config_sha256` it was created with (line 159), and nothing re-keys it. `tick()` (line 162) and `reset()` (line 272) would then raise `controller_config_changed` for that slot indefinitely. A re-key path has to ship before any worker image changes. The `live-20260923b` packs are withdrawn: `idle_fault()` retries errors with no code, so cursor stops claiming about 180 s after start.

### Verified 2026-09-23 15:45Z: verify_fleet 3/3

`tools/verify_fleet.py` passed three consecutive five-agent rooms on controller `00010-dqd`: `b01e6fa0`, `924cab28`, `6d12d89e`. All five agents exited 0 with the exact `NAME | OK` text, and there were no resets; the controller replaced every worker between rooms. Room `be33dc83` (queued before the outage) was also served 5/5 as supporting evidence. The chained run removed `RUNCREW_RESET_ENABLED` on pass (revision `00011-625`, same image and runtime account). Per the keep-on decision above, the operator is re-enabling it until the re-key controller and the `live-20260923c` packs ship.

### Incident record 2026-09-23 16:05-20:13Z: Claude account spend limit; claude slot blocked for four hours

- `runcrew-worker-claude-2p6f9` (16:08:44Z, room `17b89ba3`), `59q4g` (16:16:13Z, room `71ef7d07`) and `wdbn9` (16:25:15Z, room `d84869d7`) each claimed a room and failed it 1-4 s later with `model_call_attempted=True`, recorded as `native_or_connection_failure`. Each committed and released its credential cleanly (every broker call returned 200). The third failure hit `MAX_CONSECUTIVE_FAILURES`, and the slot was `blocked` (`worker_failed_no_restart_loop`) from 16:26:00Z. It was not quarantined. (Light's diagnosis.)
- Cause: the Claude account's monthly spend limit, hit at about 16:05Z and reset at 20:00Z (1pm America/Los_Angeles). The operators' own Claude Code sessions hit the same limit in the same window, so the fleet worker and those sessions appear to share one limit. Heavy Claude-side automation can take the fleet's claude worker offline, so bulk work goes to Cursor.
- Why the code was generic: `ClaudeRuntimeError` (the command-lifecycle checks) is not a `ProviderCodeError`, so the loop flattened it, and the deployed `live-20260922b` image predates provider codes altogether.
- Recovery: `/reset` from Retina at 20:10:47Z (generation 23). The first turn after it completed room `633def3e` at 20:12:58Z (execution `89qnw`). Rooms `17b89ba3`, `71ef7d07` and `d84869d7` need a manager retry or a resend.
- Fix, worker side (`0b6463a`, `41a1dc8`, `a45e901`):
  - claude maps definitive account-limit signals to `claude_quota_exhausted`: `billing_error`, a rejected `rate_limit_event`, and usage/spend-limit text, which is matched and never kept.
  - A bare `rate_limit` becomes `claude_rate_limited` and stays a strike.
  - Lifecycle errors keep their fixed `claude_command_lifecycle_*` codes.
  - The worker exits 75 for any `*_quota_exhausted` code.
- Fix, controller side: the section below (`b875f5e`).

### Provider quota must not burn the three strikes (2026-09-23 16:05-16:26Z)

The Claude account spend limit refused every call. Three relaunches failed within 20 minutes and that slot stayed `blocked` for four hours. The worker now exits 75 (`provider_errors.QUOTA_EXIT_CODE`) on any `*_quota_exhausted` code: the claimed room still fails, the process does not exit 1. The controller reads the execution's single task (`GET <execution>/tasks`, `lastAttemptResult.exitCode`). Exit 75 with a clean credential release does not increment `consecutive_failures`. The slot goes `idle` with `error=provider_quota_exhausted` and `next_launch_at` at 1h, then 2h, then 4h (capped). A tick during the park answers `provider_quota_parked` with `next_launch_at`, so status can show blocked on the provider rather than offline or queued. A clean success or `POST /reset` clears the park (`quota_parks` 0, and reset also drops `next_launch_at`).

The controller runtime service account needs `run.tasks.list` and `run.tasks.get` (`roles/run.viewer` covers both). Without that permission the task read fails and the controller keeps today's strike behaviour. Any other exit code, a missing code, or more than one task also keeps the strike path.

Rollout order for the re-key controller (`918f01a`, `f8826d5`): deploy it with the worker images and fleet config unchanged. Each slot then records `policy_sha256` and `slot_template_sha256` the next time it passes through idle or blocked (within about an hour, one hourly replacement). Change a worker image only after every slot has recorded them. A legacy document (only `config_sha256`) can still be re-keyed while the live job still carries the previous template; once both the job and the config have moved without a recorded template, the controller refuses (`controller_config_changed`). During an image rollout, an active slot's tick raises `controller_config_changed` until that execution ends; the idle transition that follows re-keys it.

### Incident record 2026-09-23 20:20-22:43Z: claude credential quarantined by a long turn; recovered on the d image

- `runcrew-worker-claude-wkklz` claimed stress room `11742c93` (T1) at 20:24:23Z on the `live-20260922b` image, which never renews during a turn. The turn ran about 6m40s; the 240 s broker lease expired about 20:28:16Z; at 20:31:04Z `assert_current` returned 409 and the worker quarantined the credential (`credential_cleanup_failed`). The slot blocked as `worker_failed_credential_unreleased`; `/reset` cannot clear that state.
- Live since, all deployed from Retina by its user through Cursor on Retina:
  - hub `977f8ab` as `runcrew-hub-00004-99b` (21:28:07Z, build `8cba7e29`): timeout floor, one-agent rosters honoured, relaunch warm-up and `blocked_on_provider` status labels, JSON-safe claim path. Zero 4xx/5xx after deploy.
  - controller `2f9affa` as `runcrew-live-fleet-00013-nzm` (21:35:30Z, build `1ec8c5c7`, digest `95ef8401...`): template re-key, quota park (exit 75), launch clears the park error. `roles/run.viewer` for `runcrew-credential-broker` (project level, 20:51:47Z). All five slots recorded their binding by 21:45:53Z.
  - claude `live-20260923d` (`claude-worker@sha256:57c0b26e...`, template `6ec7fe4c...`) with fleet.json v6 on `00015-nxt` (22:33:32Z). A c build (`5aafb53b`) was deployed first and superseded before any launch.
- Recovery: the owner's version-checked un-quarantine write (only the six fields; preconditions intent empty, version == last_release_version, lease expired, execution terminal; the Claude OAuth token is never rewritten by the CLI), then `/reset` at 22:40:39Z (a first attempt at 22:40:16Z had a bad token, 401, nothing executed; a later duplicate was refused `slot_not_stopped`). `runcrew-worker-claude-696xj` (gen 28) launched 22:41:01Z; its first turn was clean: three in-turn renews at 19/21/21 s, all 200, acquire -> assert -> commit -> release, no 409.
- Packs `live-20260923d` (all five) from `d07c0a6`: in-turn renew (claude), bounded broker-renew tolerance (`broker_renew.py`, all five), quota codes and exit 75, structured completion facts, SIGTERM never inside close or the completion POST, telemetry spans and capability manifest. claude and copilot rebuild from public manifests (`live-image-workers/packs/`, `rebuild_pack.py`, `53b55db`); codex, cursor and grok carry the owner pin and ship through private runcrew (`alpha/worker-packs-20260923d`, `release/`). Independent verification (Cursor on Alpha) PASS; image-layout tests pass for all five. Pack builds need `serviceAccount` runcrew-op-deploy pinned at build time (the pack cloudbuild.json has none).
- Round 2 (codex, copilot, cursor, grok to d; one fleet.json v7) was cleared by the claude canary and is in progress. Hub release 2 (runcrew `9bea79b`: step recovery with attempt records and a manager retry, agent pause, capability manifest; independent review APPROVE, 856 tests) is handed to Retina; rollback to `977f8ab` after `tools/release2_rollback_check.py` (read-only) per `docs/RELEASE2.md`.
- Process notes: before relaying a failure that someone might act on, check for a newer success (the 401 alert caused one harmless duplicate reset). Secret-store and credential writes stay the owner's own actions.
