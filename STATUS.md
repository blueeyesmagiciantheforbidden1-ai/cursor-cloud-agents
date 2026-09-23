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
