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

- The failed room's real code was `project_work_only` (`live_loop.py` `task_prompt`), which fires before any provider or deadline check. The room lacked `purpose == 'project'`; the hub revision serving since then stores it. A fresh room on the current revision is the test; the old room stays failed.
- Any non-zero exit from any agent fails the whole room with no automatic retry (`core.py` completion path). Deliberate; leave it.
- Hardening still to do, deferred because `core.py`/`test_hub.py` already carry two unmerged patches (room engine on Retina, usage gate on Demand) that overlap: reject a room at `create()` when `timeout_seconds` is below what an agent on it needs. Only codex has a floor today (`FINALIZE_RESERVE` 45 + 30 in `providers/codex.py`, plus the loop's 25 s completion reserve, so about 100 s; use 120). Keep other agents at 30. Pair it with a test that derives the codex number from the adapter constants so it cannot go stale. A worker cannot enforce this itself because `timeout_seconds` only arrives inside the claim.

### Fleet capability as verified on 2026-09-22

| Machine | Host | Has | Lacks |
| --- | --- | --- | --- |
| Alpha | WIN-R7K3M9X2P6N | git with GitHub credential (fetch and push verified on both repos), this checkout, `C:\API_KEYS` pack, Python 3.12 at `AppData\Local\Programs\Python\Python312` | gcloud, hub manager token (lookup blocked as credential exploration) |
| Light | WIN-L8Q2M6V9R4K | gcloud (project visible; `run jobs executions list` works), GitHub read on both repos | `logging read` (user denied), Firestore document read (classifier denied), runcrew checkout, verified push |
| Demand / Retina | WIN-4RR6E8E6DGC (one box) | Cursor worker directories only | gcloud, any checkout, hub URL or token, GitHub credential |

No machine can create a hub room; that requires the manager token, so a live five-agent test has to start from the operator's ChatGPT connector. Cursor's split that assigned Firestore reads to Retina does not match that box.
