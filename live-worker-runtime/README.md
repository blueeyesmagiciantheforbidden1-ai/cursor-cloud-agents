# live-worker-runtime

Worker runtime for cursor-cloud-agents, including the offline MH-003
`workspace_runner` prototype (`ENABLED=False`, not wired into `live_loop`).

## Claim challenge (runner receipts)

When a hub claim envelope carries `receipt_challenge` (32 lowercase hex), the
runner copies that value into the run spec and echoes it as
`runner_receipt.challenge`. If the claim does not carry a string challenge, the
receipt omits `challenge` entirely.

Hub rooms with `require_runner_receipt` or `require_tests_green` depend on this
exact copy. Sending a challenge to a lease that has none yields
`challenge_mismatch`; omitting it when the lease has one yields
`challenge_missing`. See `runcrew/docs/MYHERO_RUNNER_RECEIPTS.md` ("Claim
challenge"). Use `runner_spec_from_task(task, base_snapshot, test_command)` to
map a claim task into a `run_coding_task` spec.
