# Fleet verification

`verify_fleet.py` checks the five-agent RunCrew fleet through the hub HTTP API.
It uses only the Python standard library. It does not call gcloud, Cloud Run, or
Firestore. When the hub sits behind Cloud Run, pass an identity token in the
environment; the script only forwards that value.

The manager token is sent as `X-Hub-Token`. The identity token is sent as
`Authorization: Bearer`. Neither value is printed, written to the ledger, or
placed on the command line.

## Run

```bash
export HUB_MANAGER_TOKEN=...
export HUB_ID_TOKEN=...
python3 tools/verify_fleet.py \
  --hub https://hub.example.run.app \
  --ledger /tmp/fleet-ledger.json \
  --consecutive 3 \
  --max-rooms 8
```

`--consecutive` defaults to 3 and `--max-rooms` defaults to 8. The process exits
0 only after that many rooms pass in a row. Any failed, stalled, timed-out, or
text-mismatched room resets the streak. The run stops at `--max-rooms` if the
streak is still short.

Each room is created alone with `POST /v1/rooms`:

- agents: `codex`, `claude`, `cursor`, `copilot`, `grok`
- `timeout_seconds`: 300
- `workspace`: `default`
- `purpose`: `project`
- prompt: each agent must reply with exactly its uppercase name followed by ` | OK`
  (`CODEX | OK`, `CLAUDE | OK`, `CURSOR | OK`, `COPILOT | OK`, `GROK | OK`)

The script then polls `GET /v1/rooms/<id>` every 10 seconds until `status` is
`completed`, `failed`, or `stalled`, or until 15 minutes have passed. It prints
each agent's `exit_code` and reply text. A room passes only when `status` is
`completed`, every agent exited 0, and the reply text is exactly `NAME | OK`
(leading and trailing whitespace is ignored).

The ledger is rewritten after every room. Each entry has `room_id`,
`created_at`, `exit_codes` for the five agents (`null` if that agent never
replied), and `pass`. The top-level `result` is `pass` only when the streak
reaches `--consecutive`.

Loopback `http://127.0.0.1`, `http://localhost`, and `http://[::1]` are accepted
for local tests. Any other hub URL must be `https`, with no userinfo, query, or
path. Redirects are not followed.

## Tests

```bash
python3 -B tools/test_verify_fleet.py
```

The tests start a local `http.server` fake hub. They cover a five-for-five pass,
a failed agent, a stalled room, and a failure that resets the consecutive
counter. They do not call Cloud Run, Firestore, or a live hub.
