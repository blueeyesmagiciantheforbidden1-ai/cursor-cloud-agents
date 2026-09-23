# Assignments to the external Claude Code, Cursor, and GitHub Copilot agents

## Current handoff — 2026-09-20, 22:35 UTC

The historical organization restrictions below are superseded by the user's later instruction to use the existing Google Cloud setup. Current deployment facts and exact target are in `deploy/CURRENT_STATE.md`. MyHero is connected in the first ChatGPT account: Chrome shows six manager actions, and a real ChatGPT `hub_status` call returned the live Cloud Run/Firestore status. The production workers remain **0/5 connected**; local development contributions do not count as cloud workers.

Current bounded Codex implementation assignments: Claude subscription preflight (`subscription_auth.py`, narrow `worker.py` integration, related tests); distinct frontier-model selection (`model_policy.py`, policy tests/docs); official cloud authentication research (`docs/CLOUD_AGENT_AUTH.md`). Root owns cloud worker packaging/deployment and integration. Existing external follow-up receipts below remain historical evidence, not new task acknowledgments or proof of connected cloud agents.

The newest user requirement is a diverse best stack: strongest available eligible models, highest supported effort, no duplicate underlying model across agents, and refreshed selection after provider updates. Preserve subscription/cost checks and show unavailable capabilities honestly.

## Current handoff — 2026-09-20, 19:15 UTC

The earlier framing/identity issues below have been fixed and verified. Real Copilot CLI and Cursor SDK prompt-only contributions are recorded in `live-pilot-result.json` and `cursor-sdk-review.json`; the SDK sessions are separate from desktop chats.

**CLAUDE-003, queued until acknowledged:** In your existing separate test folder, independently re-run the HTTP and stdio tests against the latest hub. Check that the read-only status credential cannot mutate rooms or enter MCP, and that timeout results are delivered before the authoritative deadline while expired leases remain rejected. Report results and the exact tested snapshot in CLAUDE-FOLLOWUP.md. Do not edit shared implementation files.

**Cursor review already received:** The SDK review highlighted holdout-family/retrieval leakage, uncounted retries/speculation, and behavioral rather than configuration diversity. These caveats are being incorporated into the local lab documentation. No desktop session is claimed to be active merely because its app is running.

Cloud direction changed: use the generated Cloud Run `run.app` address, no domain purchase. A completely NEW organization remains mandatory. Both existing organizations `833782852711` and `1091262552755` are excluded. The user is choosing an unused Google account for Google's standalone-organization route. No cloud resources have been created.

Codex has read CLAUDE-FOLLOWUP.md, CURSOR-FOLLOWUP.txt, and COPILOT-FOLLOWUP.txt and acknowledges your actual contributions. Thank you for keeping changes separate. These are user-authorized follow-up assignments; do not recursively delegate just to satisfy Codex's Grok preference.

## Resume handoff: Codex temporarily owns the MCP fix

Codex resumed after the interruption. The follow-up files still contain the original review and the stdio framing bug remains in the code. Codex is completing `agent_hub/mcp.py`, `tests/test_mcp.py`, and connector integration now. Cursor: please review the resulting change and report in your follow-up file; do not concurrently modify those files until ownership is handed back. Claude's independent client checks and Copilot's correctness review remain assigned below.

Codex also owns `agent_hub/adapters.py` and `tests/test_worker.py` for this integration pass. The installed Claude 2.1.275 help explicitly confirms --safe-mode preserves auth and --bare skips OAuth. GitHub's permission documentation distinguishes --available-tools=view,grep,glob (tool identifiers) from --allow-tool=read (permission category). Review findings must preserve that distinction; do not reintroduce --bare or --available-tools=read without an actual runtime test proving it correct.

## Cursor: MCP integration review

Review `agent_hub/mcp.py`, `tests/test_mcp.py`, and `connectors/` for this pass. The original implementation checklist is:

1. Fix the stdio framing bug Claude found: use one JSON-RPC message per newline (MCP stdio), not Content-Length/LSP framing.
2. Bound input/output bytes, return valid JSON-RPC errors for malformed requests and missing rooms, and preserve notifications with no response.
3. Support the private Cloud Run deployment in the stdio bridge: refresh Google identity tokens through the existing worker's documented gcloud or GCE metadata modes, in addition to the per-agent hub token. Do not embed tokens in example files or static command arguments.
4. Test a real initialize/tools/list/tool-call cycle over stdio and HTTP. Coordinate with Claude by appending your status to CURSOR-FOLLOWUP.txt.

Do not edit core.py/store.py/server.py/worker.py in parallel. Report required integration changes and Codex will apply them.

## Claude Code: independent end-to-end verification owner

Keep your separate `API_KEYS/agent-hub-claude-worker/` test area. Re-run your HTTP and stdio MCP tests after Cursor's change; report exact commands, outcomes, and version-dependent CLI flags. Read the actual installed --help when it differs from public docs.

The main adapter currently uses --safe-mode, NOT --bare. The separate run_claude_review.py probe used --bare deliberately with an API key. Subscription workers must retain normal OAuth access. The packaged Windows CLI path visible from Codex is under AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/claude-code/2.1.275/claude.exe.

Please also identify the actual source location of Retina's self-improvement loop if your existing session has access. Codex found only an RDP endpoint in the saved screenshot, no local Retina repo or command connection. Do not copy credentials or unrelated server data. Do not describe generic ideas as verified Retina behavior.

## GitHub Copilot: correctness review owner

Review the current hub after the queue/UTF-8/attempt-budget fixes. Focus on runtime issues in retry, cancellation, byte limits, and API/MCP authorization. Add a focused regression test in `tests/test_copilot_review.py` if a confirmed bug needs one; do not change implementation files until Codex assigns the fix.

Validate adapter disagreements against the installed Copilot binary: `C:/Users/9/AppData/Local/github-copilot-sdk/cli/1.0.84-5/copilot.exe`. Distinguish underlying tool IDs (view/grep/glob) from approval categories (read/write/shell). Do not replace flags based only on a terminology mismatch. Claude's installed --help confirms --safe-mode and --tools are real flags.

## Shared requirements

- New isolated Google Cloud organization remains mandatory; no deployment to existing organization 833782852711.
- No domain purchase, identity creation, billing changes, or cloud provisioning in this pass.
- Keep credentials outside source, messages, build contexts, and test outputs.
- Describe results accurately: actual model contributions, simulated worker results, source inspection, and live-cloud verification are different evidence.
- Record replies in your existing follow-up file or your own file under collaboration/. Do not overwrite another agent's files.
