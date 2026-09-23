# External team assignments — awaiting individual acknowledgments

These assignments are for the actual Claude Code, Cursor, and GitHub Copilot sessions. Internal Codex helpers are not acknowledgments from those products. A file handoff is queued until its named agent confirms receipt.

## Cursor: dashboard review (CURSOR-002)

Review the status website's usefulness after `agent_hub/web/` is ready: agent health, last check, unavailable vs zero credits, stale usage, session usage vs account balance, failed connection state, mobile readability, and actionable alerts. Read-only review; do not change the dashboard owner's files. Put concrete findings and your actual checks in `collaboration/cursor-dashboard-review.md`. First append `CURSOR-002 acknowledged` and a timestamp to `CURSOR-FOLLOWUP.txt`.

## GitHub Copilot: telemetry correctness (COPILOT-002)

After `agent_hub/telemetry.py` is available, review status/usage validation, stale data, agent identity isolation, duplicate reports, and failure handling. Write a focused regression only for a confirmed issue in your own `tests/test_external_copilot.py`; do not edit implementation files. First append `COPILOT-002 acknowledged` and a timestamp to `COPILOT-FOLLOWUP.txt`. Provide results in `collaboration/copilot-telemetry-review.md`.

## Claude Code: Retina backup source and independent MCP checks (CLAUDE-002)

The user says the self-improvement system is in backup ZIPs, Stage 1, called Research something. Locate the archive and exact source members using existing authorized access; inspect names before extracting. Do not run archived code or share credentials. The local Desktop/Downloads/Documents backups checked by Codex did not match. Record provenance and findings in `collaboration/claude-retina-source.md`. Also rerun your real-client MCP connection test against the repaired bridge. First append `CLAUDE-002 acknowledged` and a timestamp to `CLAUDE-FOLLOWUP.md`.

## Boundaries

- The source-review assignments are prepared; an automatic approval review requires explicit confirmation before Codex launches a provider task that sends project source files to that provider. Prompt-only work with no source access can proceed.
- Read only the new hub source needed for the assignment, never the API_KEYS credential directory. Do not send credentials, provider keys, unrelated source, or whole conversation histories.
- No cloud provisioning, domain purchase, installation, or production changes in these assignments.
- Report `acknowledged`, `running`, `completed`, or `blocked` truthfully with evidence. A running desktop process alone proves none of these task states.
