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
