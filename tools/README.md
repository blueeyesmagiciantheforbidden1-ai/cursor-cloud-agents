# Fleet verification

`verify_fleet.py` checks the five-agent RunCrew fleet through the hub HTTP API.
It uses only the Python standard library. It does not call gcloud, Cloud Run, or
Firestore. When the hub sits behind Cloud Run, pass an identity token in the
environment; the script only forwards that value.

The manager token is sent as `X-Hub-Token`. The identity token is sent as
`Authorization: Bearer`. Neither value is printed, written to the ledger, or
placed on the command line. The same rule covers the per-room nonce and the
per-agent expected tokens.

A room never passes because a worker says it passed. Each prompt carries a
coordinator-chosen nonce and one token per agent, shaped `NONCE-AGENTNAME`
(for example `0123456789abcdef-CODEX`). Validation matches only when that
agent's reply, aside from surrounding whitespace, is exactly its token.

## Gates

Run the gates in this order:

`capability -> roster -> fleet -> duplicate -> load -> expiry`

`all` runs that sequence and stops on the first failing gate. A skipped expiry
gate is not a failure. With no subcommand, the fleet gate runs, and the flags
below keep working.

```bash
export HUB_MANAGER_TOKEN=...
export HUB_ID_TOKEN=...
python3 tools/verify_fleet.py all \
  --hub https://hub.example.run.app \
  --ledger /tmp/fleet-ledger.json
```

One gate:

```bash
python3 tools/verify_fleet.py fleet \
  --hub https://hub.example.run.app \
  --ledger /tmp/fleet-ledger.json \
  --consecutive 2 \
  --max-rooms 8
```

| Gate | What it checks |
| --- | --- |
| capability | `GET /v1/status`. All five agents are `ready` or `restarting`, and none are `offline`. Any capability manifest fields the hub sent (`capability_manifest`, `manifest`, `capabilities`) are copied into the ledger. Missing manifest fields stay empty. |
| roster | One room per agent. The effective roster is exactly that agent and the room has one message from them. Hubs that only return `agents` use that list. |
| fleet | Five-agent rooms. Exit 0 after two consecutive passing rooms by default. |
| duplicate | A five-agent room in which no agent message appears twice. |
| load | N five-agent rooms are created together, then polled. Default 3, allowed 1 through 5. All must finish inside `--room-timeout`. |
| expiry | Runs only when the hub reports `queue_deadline` support (`queue_deadline_supported`, `queue_deadline`, or that name in `features` / `capabilities` / `supports`). Otherwise the ledger entry is `skipped` with a reason. When support is reported, the gate passes only if the room status becomes `expired`. |

Room creation still posts only the existing fields: `prompt`, `agents`,
`timeout_seconds`, `workspace`, and `purpose`. Newer response fields are
optional. `room_status` is the hub's status string, including
`blocked_on_provider`, `retry_scheduled`, `needs_reconciliation`, and
`expired` when the hub sends them. Those states end the poll. A room gate
passes only on `completed` plus a clean execution and a matched token, except
expiry, which passes only on `expired`.

For each agent the ledger records:

- `execution_status`: `exit_code`, `delivered`, and `ok` (delivered and every copy exited 0)
- `validation_status`: `token_matched`, `wrong_token`, `missing`, or `duplicate`

Exit 0 with the wrong token is execution ok and validation failed. The gate fails.

## Flags

- `--hub` (required): base URL. Loopback `http://127.0.0.1`, `http://localhost`, and `http://[::1]` are accepted for local tests. Any other hub URL must be `https`, with no userinfo, query, or path. Redirects are not followed.
- `--ledger` (required): JSON ledger path. It is rewritten as gates and fleet rooms finish.
- `--consecutive`: fleet passes in a row (default 2). Must be less than or equal to `--max-rooms`.
- `--max-rooms`: fleet stop (default 8).
- `--room-timeout`: each room's `timeout_seconds` (default 300, allowed 120-900). The load gate also requires every room to complete within this many seconds.
- `--load-rooms`: rooms in the load gate (default 3, allowed 1-5).

The ledger is one object: `result` (`pass`, `fail`, or `skipped`) and `gates`.
Each gate entry has `gate`, `result`, `pass`, and `evidence`. Expiry's skipped
entry has `pass` null and a `reason`.

## Tests

```bash
python3 -B tools/test_verify_fleet.py
```

The tests start a local `http.server` fake hub. They cover every gate, a worker
that exits 0 with the wrong token, a duplicate message, a stalled room,
`blocked_on_provider`, and hubs that omit the new fields. They do not call
Cloud Run, Firestore, or a live hub.

`--room-timeout SECONDS` sets each room's `timeout_seconds` (default 300, allowed 120-900; the hub refuses less than 120 with codex on the room). Until the `live-20260923c` worker images are deployed, use `--room-timeout 180`: the deployed claude worker does not renew its credential lease during a turn, and a room deadline of 150-180 s ends a slow turn inside that lease instead of quarantining the credential.
