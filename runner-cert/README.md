# Runner certification (suite `runner-cert-v1`)

`runner_cert.py` checks whether a coding-agent runner on a host can read
files, write files, run commands and run tests (MyHero P0 items 26-27,
MH-003). Only checks run by the harness count. What the agent says in its
reply, and any file it writes to report success, are never proof.

It uses only the Python standard library and runs on Windows and Linux. It
needs `git` on PATH.

## What one run does

1. **Workspace.** The harness makes a fresh run directory under `--work-root`
   and refuses any path that resolves outside it, through `..` or a link. In
   it, it builds a small git repository and commits it as the base commit:
   - `calc.py`: a stub `summarize()` to implement. The docstring holds a
     checksum modulus chosen at random for this run.
   - `test_calc.py`: five visible tests.
   - `gen.py`, `CHALLENGE`, `salt.txt`: `CHALLENGE` holds a fresh random
     nonce and `salt.txt` a fresh random salt. `python gen.py` writes
     `OUTPUT.txt = sha256(nonce + salt)`.
   - `TASK.md`: the instructions. Implement `calc.py`, run the visible tests,
     copy their summary lines into `TEST_RESULT.txt`, run `gen.py`, and change
     nothing else.

   The prompt only says to read `TASK.md` and follow it. The task itself is
   only in the workspace files.
2. **Agent.** The chosen adapter runs the CLI with a hard wall-clock timeout.
   On timeout the harness kills the whole process tree (`taskkill /T /F` on
   Windows, the process group on Linux).
3. **Checks, after the agent has exited.** The harness takes one snapshot of
   the workspace and judges only that snapshot:
   - **Changed files.** It rebuilds the base in its own git repository from
     its in-memory copy of the base files and overlays the snapshot. It then
     lists changes with `git diff --cached` and hashes the binary patch. It
     never runs git against the workspace's own `.git`, which the agent
     controls. It reads only that `.git`'s `HEAD`, as plain files, to see
     whether the agent moved it. Only `calc.py`, `TEST_RESULT.txt` and
     `OUTPUT.txt` may change. These count as scope violations: any other
     added, modified or deleted file; a link or junction; a nested `.git`; a
     moved `HEAD`; git's listing disagreeing with the harness's own file
     comparison. The one exception is `__pycache__/*.pyc` for the workspace's
     own modules, which is ignored.
   - **Trusted tests.** The harness copies the agent's `calc.py` next to its
     own copy of the visible tests. It then runs them with the Python
     interpreter that started it (`-E -s -B`). Hidden cases (fixed edge cases,
     eight random inputs that depend on the modulus, and eight invalid inputs)
     go to a probe process on stdin after the agent has exited. The expected
     answers never leave harness memory. They are not written to disk, and
     the workspace never sees them.

## What each verdict means

| Capability | Verified when | Why it cannot be claimed |
| --- | --- | --- |
| `read_files` | The agent's `summarize` returns the right checksum on all eight random hidden inputs whose checksum depends on this run's modulus. | The modulus is random per run and is written only in the workspace's `calc.py`. The chance of guessing it is 1 in 60,000. |
| `write_files` | The harness's own listing shows `calc.py` modified and `TEST_RESULT.txt` and `OUTPUT.txt` created. | It comes from the snapshot, not from the agent's report. |
| `shell` | `OUTPUT.txt` equals `sha256(nonce + salt)`, which the harness recomputes. | The value cannot be produced without running code. An agent that only writes files can only guess it. |
| `tests` | All three hold: `TEST_RESULT.txt` looks like a real unittest summary (`Ran 5 tests in ...s` and `OK`, no `FAILED`); the trusted visible run passes 5/5; every hidden case passes. | `TEST_RESULT.txt` alone is only the agent's claim. The trusted run and the hidden cases are the proof. |

`certified` is true only when all four capabilities are verified, the scope is
clean, the adapter exited before the timeout, and the harness had no errors of
its own. A non-zero exit code alone does not fail a run: cursor-agent can
crash on exit (a libuv assertion) after it has finished its work. The exit
code is still recorded. `reasons` lists fixed codes: `adapter_timeout`,
`adapter_unavailable`, `adapter_error`, `scope_violation`, `harness_error`,
`<capability>_not_verified`.

## Running it on a host

```bash
python runner_cert.py --adapter cursor --runner-id alpha-cursor \
  --work-root C:\runner-cert\work --registry C:\runner-cert\registry.json --timeout 1200
```

It prints the receipt as JSON. The exit code is 0 when certified, 1 when not,
and 2 when the request was refused (bad runner id, work root, registry, or no
git). `--timeout` accepts 60-7200 seconds. The run directory, with the
workspace, `agent-output.log` (capped at 8 MiB) and the harness's own logs,
stays on disk for audit. Delete it when you are done.

To query the registry:

```bash
python runner_cert.py --status --registry C:\runner-cert\registry.json [--runner-id alpha-cursor]
```

From Python, `latest_certification(registry, runner_id)` returns the runner's
newest receipt when it certifies the runner now, and `None` otherwise. **The
newest receipt decides.** A later failed or unavailable run withdraws an
earlier certification. An expired receipt (`expires_at` = `finished_at` + 14
days), or one whose time cannot be parsed, is not a certification. The
registry keeps the last 1000 receipts appended, is written atomically under a lock
file, and is never overwritten if it cannot be read.

## Adapters

| Adapter | Command | Status |
| --- | --- | --- |
| `cursor` | `node.exe index.js -p --output-format text --trust --sandbox disabled --auto-review --workspace <ws> <prompt>`. On Windows it uses the newest `%LOCALAPPDATA%\cursor-agent\versions\*` that has `node.exe` and `index.js`, as `C:\agent-bus\cursor-worker.ps1` does, with the same argument quoting. On Linux it uses `cursor-agent` from PATH. `CURSOR_INVOKED_AS=cursor-agent`. | Flags checked against `--help` of 2026.09.23-86fc751 |
| `claude` | `claude -p --output-format text --permission-mode auto --permission-prompts none --no-session-persistence <prompt>` | Flags checked against `claude --help` 2.1.280 |
| `codex` | `codex exec --sandbox workspace-write --ephemeral --color never -C <ws> <prompt>` | Flags checked against `codex exec --help` 0.153.4 |
| `grok` | `grok --output-format plain --permission-mode auto --disable-web-search --no-subagents --cwd <ws> -p <prompt>` (the native `grok.exe`, no shim) | Flags checked against `grok --help` of 1.0.24 (68e414c661e3) on dumpling. No `--always-approve`. Pinned: any other installed version reports `cli_flags_unverified` and is never run. |
| `copilot` | none | Detection only. The CLI is not installed on any fleet host, so no flag was verified. The adapter reports `cli_not_found` or `cli_flags_unverified` and never runs the CLI, not even `--version`. |
| `fake` (tests) | an in-process function | Test double for `test_runner_cert.py` |

On Windows the harness never runs a `.cmd` shim, because `cmd.exe` would parse
the prompt again. For npm-installed `claude` and `codex` it runs
`node <package script>` directly. If it cannot find that script, it reports
`cli_shim_unresolved`.

The `claude` and `codex` flag choices are a permission policy that should be
reviewed before the first real run. `claude` uses `auto` with prompts refused,
which is the closest match to cursor's `--auto-review`. `codex` uses codex's
own `workspace-write` sandbox. A failed first run shows up as not certified.
It never shows up as a false pass.

## What a receipt is, and what it is not

A receipt is **evidence produced by trusted code on that host at that time**.
It records the harness's own SHA-256 (`harness.sha256`), the Python version,
the CLI version when it is known, the host name, the base commit, the patch
digest and the nonce, so the run directory left on disk can be checked
against it.

It is **not an attestation.** Nothing is signed, and nothing binds it to
hardware or to an identity. Whoever controls the host could edit the registry
or the harness. It says that this harness saw this runner do these things
once. It says nothing about isolation, secrets or network egress (MH-002). It
does not say the runner will behave the same way on a different task.

Known limits:

- The trusted run imports the agent's `calc.py` with the harness's
  interpreter, so agent code runs once more on the host. The agent already
  had a shell there. To pass, that code has to return the right answers to
  inputs it had never seen, so faking the hidden check means solving the
  task.
- On Windows, a process that the agent started in the background and that
  outlives the agent is not killed after a normal exit. On Linux the process
  group is. The snapshot is taken when the agent exits, so anything that
  process changes later is not judged.
- Tools that write their own files into the workspace (a `.pytest_cache/`,
  an editor folder) count as scope violations. `TASK.md` asks for `unittest`
  for this reason.

## Tests

```bash
cd runner-cert
python -m unittest -v test_runner_cert
```

The tests use no network and no real agent CLI. Fake adapters play an honest
agent, one that guesses `OUTPUT.txt`, one that edits the tests or adds or
deletes files, one that commits, one that leaves a symlink, one that fails the
hidden cases, one that forges `TEST_RESULT.txt`, and ones that skip one of the
three files `write_files` needs. The timeout, crash and harness-error agents
first do the whole task honestly, so only that gate can refuse them. Every
certify test also checks that a receipt is certified exactly when `reasons`
is empty. Other tests cover a `calc.py` that makes the trusted visible run skip
or drop tests, the registry (fresh, expired, the expiry instant,
newest-decides, damaged), refused paths, cursor resolution and Windows
argument quoting.
