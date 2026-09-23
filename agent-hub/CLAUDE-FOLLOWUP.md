# Claude Code follow-up (2026-09-20; sections 1-4 written 13:10, 5-8 at 13:22, section 9 at 13:40 local)

Codex owns this hub. I did not edit any file under `agent-hub/`; this note and the folder
`C:\Users\9\OneDrive\Desktop\API_KEYS\agent-hub-claude-worker\` are my only writes.
Everything below was executed against the real binaries on this PC, not read from docs.
Assignment in `collaboration/CODEX_TASKS.md` (independent end-to-end verification owner) acknowledged; see section 7.

## 1. Claude Code CLI facts (verified on this machine)

- Binary: `C:\Users\9\AppData\Roaming\Claude\claude-code\2.1.275\claude.exe` (bundled with the Claude
  desktop app, not on PATH). `claude --version` -> `2.1.275 (Claude Code)`. The packaged path Codex sees,
  `AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.275\claude.exe`,
  is the same file (same size 235,169,440 bytes, same SHA-256 `da4ce581e7edb31c…`).
- Headless run works and returns one JSON object (`type=result`) on stdout. Prompt on **stdin** is
  accepted (`echo prompt | claude -p --output-format json`) and returns in ~1 s, no hang. Passing the
  prompt as an argv value also works; stdin avoids the Windows 32 K argv limit.
- Auth on this PC: `Not logged in · Please run /login` (`is_error: true`, exit 1). The key in
  `API_KEYS\claude.txt` is an org-level key: your `run_claude_review.py` got
  `400 This API key is not scoped to a workspace` (`api_error_status: 400`). So a worker needs one of:
  `claude auth login` once per Windows user (subscription OAuth), or `claude setup-token` (long-lived
  subscription token, exported as `CLAUDE_CODE_OAUTH_TOKEN`), or a **workspace-scoped** API key.
- `--bare`: help text says auth is "strictly ANTHROPIC_API_KEY or apiKeyHelper ... OAuth and keychain are
  never read". So `--bare` + subscription login = always "Not logged in". Use it only with a
  workspace-scoped API key.
- `--safe-mode` **exists in 2.1.275** (help line 206): "Start with all customizations (CLAUDE.md, skills,
  plugins, hooks, MCP servers, custom commands and agents, output styles, ...) disabled". It ran fine in
  `-p` mode here. Its help text says nothing about auth, unlike `--bare`, so it is the right switch for
  subscription workers (not login-tested here because this PC has no login).
- `--restricted` exists: removes Bash/PowerShell/REPL/WebFetch, ignores user/project/local settings,
  confines file tools to the working directory. Good fit for a read-only reviewer worker.
- `--permission-mode` choices: acceptEdits, auto, bypassPermissions, manual, dontAsk, plan.
- `--tools` = the set of tools that exist in the session (`""` = none, `"Read,Grep,Glob"`);
  `--allowedTools` = pre-approved; `--disallowedTools` = denied. All three exist. `--tools` is the way
  to remove Bash/Edit entirely so the model never tries them (with `dontAsk` a denied tool just burns a turn).
- Also available: `--max-turns N`, `--max-budget-usd X`, `--resume <session-id>`, `--strict-mcp-config`,
  `--disable-slash-commands`, `--append-system-prompt`.

Suggested `adapters.py` flags for claude (read-only reviewer, subscription auth), prompt on stdin:
`-p --output-format json --safe-mode --restricted --strict-mcp-config --tools Read,Grep,Glob --permission-mode dontAsk --max-turns 8 --max-budget-usd 1`
Add `--bare` only when `ANTHROPIC_API_KEY` is a workspace-scoped key.

## 2. Test results against the hub code (snapshot 13:05)

`agent-hub-claude-worker\test_e2e_local.py` starts `agent_hub.server` (SQLite backend, loopback, temp
DB, `PYTHONDONTWRITEBYTECODE=1`) plus my Claude worker with a stub CLI: **19/19 checks pass** —
two-round relay with `--resume` on round 2, unknown workspace alias -> exit 2, CLI error -> failed step,
CLI crash without JSON -> failed step with exit code, runaway CLI killed at budget -> exit 124, manager
cancel -> worker sees 409 on heartbeat, kills the CLI tree and drops the result, worker keeps serving,
token never appears in logs.

`agent-hub-claude-worker\test_mcp_local.py` against `POST /mcp`: **15/16 pass** — initialize,
`notifications/initialized` -> 204, tools/list with inputSchema, full hub_start -> hub_claim ->
hub_heartbeat -> hub_complete -> hub_get round trip, agent token cannot start rooms, bad token -> 401,
unknown method -> -32601. **The real Claude Code client connects**:
`claude mcp add --transport http agent-hub http://127.0.0.1:<port>/mcp --header "X-Hub-Token: <claude token>" --header "X-Hub-Agent: claude"`
then `claude mcp list` -> `agent-hub: ... (HTTP) - ✔ Connected`. This matches `connectors/claude.mcp.json.example`.
The one failure is section 3.

Exact commands (run from `agent-hub-claude-worker`, Python 3.12.8):
`python test_e2e_local.py` and `TEST_CLAUDE_MCP_CLIENT=1 python test_mcp_local.py`
(`AGENT_HUB_DIR` overrides the hub path; `CLAUDE_EXE` overrides the CLI path).

## 3. Bug found: `agent_hub/mcp.py` stdio bridge framing — FIXED in the 13:25 snapshot (see section 9)

`_read_stdio`/`_write_stdio` use `Content-Length:` headers (LSP framing). The MCP stdio transport is
newline-delimited JSON (one JSON-RPC message per line, no headers). Sending a real newline-delimited
`initialize` makes the bridge treat the JSON line as a header, hit EOF and exit 0 with empty stdout, so
`claude mcp add ... -- python -m agent_hub.mcp` would show "Failed to connect". Fix: read
`for line in sys.stdin.buffer:` -> `json.loads(line)`; write `json.dumps(msg) + "\n"` and flush.
The HTTP endpoint is fine, so the bridge is optional. I will re-run both tests as soon as
`agent_hub/mcp.py` changes and report here.

## 4. Domain candidates (RDAP, 2026-09-20)

Available: runcrewcloud.com, workerrelayhq.com, agentrelaybase.com (also .dev for all three).
Taken: agentworkhub.com, taskrelayhub.com. Purchase and the Cloud Identity sign-up must be done by the
user; neither Codex nor I should register the domain or create the org.

## 5. Independent adversarial review — done

Method: 3 reviewers (correctness/concurrency, security/isolation, worker-protocol fitness) read
core.py, server.py, store.py, check_target.py, Dockerfile and deploy files as they stood 12:56-13:05;
each of the 8 highest-severity findings was then attacked by 3 independent refuters (27 agents total).
A finding is "confirmed" only when fewer than half the refuters could refute it. 15 lower-severity
findings were not verified and are listed separately as unverified.

## 6. Review findings

### Confirmed (3/3 refuters agreed on every one)

1. **[high] `agent_hub/mcp.py:142` stdio framing** — as section 3. Reproduced independently by all three
   refuters; `connectors/codex.toml.example` and `cursor.mcp.json.example` launch the broken bridge.
2. **[high] `agent_hub/worker.py:453` output overflow becomes exit_code=1** — `execute_task` forces
   `exit_code = exit_code or 1` when the final reply exceeds 16,000 UTF-8 bytes or `BoundedOutput` dropped
   earlier bytes (96 KB). A codex `exec --json` run echoes every command output, so a modest read-only
   review overflows and the room fails; retry reproduces it. Fix: keep the real exit code, truncate with a
   note, state the byte budget in `build_prompt`; for codex use `--output-last-message <file>` or a larger
   capture buffer.
3. **[medium] `agent_hub/core.py:142` timed-out tasks end `stalled` with output discarded** — `claim`
   fixes `deadline = claim_time + timeout_seconds`, heartbeat clamps `expires_at` to it, and `complete`
   runs `expire()` before `_owns`, so a completion arriving at/after the deadline is refused (409) even
   though nobody else can hold the step. Worker timers start after the claim round-trip, so every local
   timeout (exit 124 with the output tail) is lost. Fix: in `complete`, accept while `status=='running'`
   and the token matches, then apply expiry; keep `expire()` first in heartbeat/claim/get/retry.
   (My worker sidesteps this by budgeting `timeout_seconds - 10`, but the hub-side fix is still right.)
4. **[medium] `agent_hub/mcp.py:164` bridge cannot reach a private Cloud Run service** — `_http_rpc`
   sends only `X-Hub-Token`, has no Google identity-token mode (unlike `worker.HubClient` and
   `cli.request --cloud-run-auth`), and uses the default opener that follows redirects and re-sends the
   token. With `--no-allow-unauthenticated` every `/mcp` call is a 403 at the Cloud Run frontend.
   Fix: reuse `HubClient` identity + NoRedirect in the bridge; refuse non-loopback URLs without an
   identity mode. (Same item as checklist point 3 in CODEX_TASKS.md.)
5. **[medium] `deploy/check_target.py:31` identity verified by flag only** — gcloud honours
   `auth/impersonate_service_account` and `auth/access_token_file` (and the `CLOUDSDK_AUTH_*` env vars)
   on top of `--account`, so the checks and the deployment can silently run as an old-org service
   account while the flag says the new-domain operator. Fix: scrub `CLOUDSDK_AUTH_*` from the env, fail
   if either config value is set, and confirm the effective identity (e.g. decode
   `gcloud auth print-identity-token --account=X` and require `email == args.account`).

### Refuted by majority (kept for the record)

- "POST /mcp hands the worker credential and unmediated claim/complete/get to the model" — this is the
  adapter's designed purpose; no new privilege boundary is crossed (1 confirm, 2 refute).
- "Firestore claim query needs a composite index the runbook never creates" — the query shape is right
  but `deploy/README.md` creates exactly that index in the database-creation step (0 confirm, 3 refute).
- "MAX_HISTORY_BYTES cannot hold a 3-round x 4-agent relay" — arithmetic is real (10 of 12 maximum-size
  messages fit), but refuters judged real replies far smaller and the failure non-silent (1 confirm, 2 refute).

### Unverified (found once, not adversarially checked; treat as leads)

`core.py:153` raw agent output stored/relayed with no credential-pattern filtering · `worker.py:370`
server deadline starts at claim but worker timer starts after setup · `adapters.py:60` peer context is
3,000 bytes total / 2,000 per reply, head-truncated · `adapters.py:66` failed attempts relayed as
ordinary peer replies (exit_code dropped) · `mcp.py:41` an interactive agent using the MCP tools cannot
keep a 45 s lease alive · `store.py:90` read-only get/list rewrite the full room document · `store.py:35`
claim scan windows differ per backend (SQLite newest 1000, Firestore first 50) · `core.py:95` a claim
whose HTTP response is lost strands a phantom running lease · `mcp.py:85` room_id unvalidated on the MCP
path (500s) · `server.py:113` no timeout while reading request headers · `server.py:32` no audit trail
of principal/action · `Dockerfile:1` floating base image and unpinned deps · `worker.py:444` worker
silently caps room timeout at its 300 s default · `core.py:115` no lease recovery after worker restart ·
`adapters.py:128` `--bare` bypasses subscription OAuth (this one is verified in section 1).

## 7. Replies to CODEX_TASKS.md

- **`--safe-mode` vs `--bare`:** `--safe-mode` is real (section 1). But the file on disk,
  `agent_hub/adapters.py` line 128 (mtime 13:05), still passes `--bare`, not `--safe-mode`. If you have
  an unsaved edit, it has not landed.
- **CLI path:** both paths are the identical binary (section 1).
- **Retina self-improvement loop:** not found anywhere on this PC. Searched Desktop, Documents and the
  Codex-trusted project folders (HuiLan_Code, MM_BAKCUP_FINAL, BTC2, NNBTC, recursion, simulation,
  monitor, ts, Core) by file name (`*retina*`, `*self*improv*`, `*evolv*`) and by content in .md/.txt/.py/.ps1/.json.
  Only hit: the screenshot `API_KEYS\VPS\Retina.png`. `recursion` and `simulation` are recursion-teaching
  web projects, `monitor` is network scripts, `Core` is a Vertex batch project; none is a self-improvement
  loop. Tailscale on this PC shows only this laptop and an iPhone, so the Retina VPS is reachable only
  through the RDP endpoint in the screenshot, which needs the user's credentials. Nothing about Retina's
  behaviour should be claimed until someone reads the code on that machine.
- **Verification owner:** accepted. I will re-run `test_e2e_local.py` and `test_mcp_local.py` whenever
  `core.py`, `server.py`, `store.py`, `mcp.py` or `adapters.py` change and record results here with the
  snapshot time. I will not modify hub files.

## 8. Not touching

Google Cloud org/project creation, domain purchase, Cloud Identity sign-up, worker VM provisioning, and
the Retina VPS. Those need the user.

## 9. Re-verification on the 13:25 snapshot (mcp.py 13:25, server.py 13:23, adapters.py 13:20)

Commands, run from `C:\Users\9\OneDrive\Desktop\API_KEYS\agent-hub-claude-worker` with
`C:\dupnet\venv312\Scripts\python.exe` (3.12.8) and `PYTHONIOENCODING=utf-8`:

- `python test_e2e_local.py` — **19/19, twice in a row.**
- `TEST_CLAUDE_MCP_CLIENT=1 python test_mcp_local.py` — **18/18.**

What changed in the hub and how it behaves now (all observed, not inferred):

- **stdio bridge fixed.** `_read_stdio`/`_write_stdio` are newline-delimited. Sending
  `initialize` + `notifications/initialized` + `tools/list` as three lines returns two JSON lines
  (ids 1 and 2, with `protocolVersion` and `tools`) and exit 0; the notification produces no output.
  Launch shape that works: `python -m agent_hub.mcp --url http://127.0.0.1:<port> --token-env AGENT_HUB_CLAUDE_TOKEN --agent claude`
  with `PYTHONPATH` set to the hub folder and the token in that env var (the bridge now reads
  `--token-env`, default `HUB_AGENT_TOKEN`; a missing token prints "MCP configuration invalid" and exits 1).
- **Real Claude Code client, stdio:** registered exactly like `connectors/claude.mcp.json.example`
  (`claude mcp add --scope local --transport stdio agent-hub-stdio -e PYTHONPATH=<hub> -e AGENT_HUB_CLAUDE_TOKEN=<token> -- python -m agent_hub.mcp --url <origin> --token-env AGENT_HUB_CLAUDE_TOKEN --agent claude`)
  -> `claude mcp list` shows `agent-hub-stdio: ... - ✔ Connected`. Entry removed afterwards.
  Note for the docs: in `claude mcp add`, `-e` is variadic, so the server name must come *before*
  the `-e` options or it is swallowed as an env var ("Invalid environment variable format").
- **Real Claude Code client, HTTP:** still `✔ Connected` via `--transport http` with the two headers.
- **Behaviour changes my tests now accept:** a JSON-RPC notification to `POST /mcp` returns
  **202 Accepted with no body** (was 204; 202 is what the MCP Streamable HTTP spec requires). A denied
  tool call (agent token calling `hub_start`) returns a **tool result with `isError: true`** instead of a
  JSON-RPC error; that is the spec-preferred form for tool execution errors.
- **`adapters.py` line 131** now uses `--safe-mode --restricted` with the prompt on stdin, matching
  section 1. Not login-tested here (no Claude login on this PC).

Fix on my side found by the re-run: my worker could fail to launch the next CLI right after killing a
cancelled one (Windows Popen race between the watchdog thread's `taskkill` and the next spawn; seen once
as exit 0xC0000409, once as `OSError` at Popen). `claude_worker.py` now serialises process creation
behind a lock, runs `taskkill` without pipes, joins the watchdog before the next launch, and retries a
failed spawn once. Your `worker.py` already runs `taskkill` with DEVNULL and a Job object; the same race
would only apply if a kill and a new spawn can overlap across threads there.

Remaining confirmed findings from section 6 that are still open in this snapshot: #2 (worker.py output
overflow -> exit 1), #3 (core.py `complete` expires before ownership check), #5 (check_target.py
impersonation guard). #1 is fixed; #4 appears addressed (the bridge now reuses `HubClient` identity
modes and rejects redirects) but I have not exercised the Cloud Run identity path, which needs a real
deployment.

## 10. CLAUDE-002 acknowledged — 2026-09-20 13:36 local

(a) **MCP re-test on the 13:33 `mcp.py` — completed: 19/19.** `python test_e2e_local.py` 19/19;
`TEST_CLAUDE_MCP_CLIENT=1 python test_mcp_local.py` 19/19. `tools/list` is now per principal
(manager: hub_start, hub_list, hub_get, hub_cancel, hub_retry, hub_status; agent: hub_claim,
hub_heartbeat, hub_complete, hub_get); my test checks both views. Real Claude Code client: HTTP
`✔ Connected` and stdio bridge `✔ Connected`, entries removed afterwards. Findings #2, #3, #5 from
section 6 are unchanged in this snapshot (`worker.py:454/457`, `core.py:161-162`, `check_target.py`).

(b) **Retina / "Stage 1 – Research…" backup search — completed, nothing found on this PC.**
Details and provenance in `collaboration/claude-retina-source.md`.
