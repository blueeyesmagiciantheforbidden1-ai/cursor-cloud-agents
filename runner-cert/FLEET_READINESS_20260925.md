# Fleet runner certification, 2026-09-25 (suite `runner-cert-v1`)

This is MyHero items 26 and 27 (P0 "sandbox runner certification"). Each row is a receipt that `runner_cert.py` wrote on that host after the agent exited. No row comes from an agent's claim. The raw receipts, with their run directories, stay on each host in `C:\runner-cert\registry.json` and `C:\runner-cert\work\`. They are not committed here, because the `output_tail` of a failed run can quote auth error text.

Harness `5fc48914…` is a4d805d. Harness `efedec52…` is 5d92e8b (the grok adapter). The trusted checks are the same in both. Every receipt expires 14 days after it finished.

| Adapter | SSH host | Hostname | CLI version | Read | Write | Shell | Tests | Certified | Agent s | Finished (UTC) | Nonce | Harness | Why not |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude | demand | WIN-4RR6E8E6DGC | 2.1.282 (Claude Code) | yes | yes | yes | yes | **yes** | 55.0 | 2026-09-25T22:47:05Z | 3a1cf4960271 | efedec525d1c |  |
| claude | retina | WIN-VI96OVQI4I6 | 2.1.280 (Claude Code) | yes | yes | yes | yes | **yes** | 58.8 | 2026-09-25T22:43:33Z | de3d7f2084b7 | 5fc48914aef9 |  |
| codex | Alpha (local) | WIN-R7K3M9X2P6N | codex-cli 0.153.4 | no | no | no | no | no | 38.2 | 2026-09-25T22:43:17Z | 04e55dbaa0ef | 5fc48914aef9 | 401 Unauthorized from the provider (auth on this host) |
| cursor | demand | WIN-4RR6E8E6DGC | 2026.09.23-86fc751 | yes | yes | yes | yes | **yes** | 82.2 | 2026-09-25T22:41:55Z | 6cfbb533f42d | 5fc48914aef9 |  |
| cursor | dumpling | WIN-VI96OVQI4I6 | 2026.09.23-86fc751 | yes | yes | yes | yes | **yes** | 61.9 | 2026-09-25T22:41:09Z | 2c93e0e37338 | 5fc48914aef9 |  |
| cursor | light | WIN-L8Q2M6V9R4K | 2026.09.23-86fc751 | yes | yes | yes | yes | **yes** | 65.9 | 2026-09-25T22:41:11Z | 3556ff06c4d1 | 5fc48914aef9 |  |
| cursor | retina | WIN-VI96OVQI4I6 | 2026.09.18-9a7762b | yes | yes | yes | yes | **yes** | 129.5 | 2026-09-25T22:42:13Z | b8dd5c3ca4ee | 5fc48914aef9 |  |
| grok | demand | WIN-4RR6E8E6DGC | grok 1.0.13 (5e9a58528b76) [stable] | no | no | no | no | no | 1.4 | 2026-09-25T22:45:38Z | abba3ffa4c1f | efedec525d1c | CLI not signed in (`grok login` needed) |
| grok | dumpling | WIN-VI96OVQI4I6 | grok 1.0.24 (68e414c661e3) | no | no | no | no | no | 3.6 | 2026-09-25T22:45:38Z | d922d858c20e | efedec525d1c | CLI not signed in (`grok login` needed) |
| grok | retina | WIN-VI96OVQI4I6 | grok 1.0.24 (68e414c661e3) | no | no | no | no | no | 8.7 | 2026-09-25T22:45:42Z | d4f2aed5b159 | efedec525d1c | CLI not signed in (`grok login` needed) |

## Readiness by agent

| Agent | Certified hosts | Status |
| --- | --- | --- |
| Cursor | dumpling, demand, light, retina | Certified on four hosts. Alpha's cursor-agent is not logged in, so it was not run there. |
| Claude | demand, retina | Certified on two hosts. dumpling's `claude` is not logged in. Light has no `claude`. |
| Codex | none | It is installed only on Alpha. Every model call returned 401: the request used a service-account API key that the provider rejected, although `codex login status` says ChatGPT. The harness correctly refused it. |
| Grok | none | The new adapter ran on dumpling, demand and retina. Each CLI answered "Not signed in", so the model never ran. |
| Copilot | none | No fleet host has the `copilot` CLI. The adapter stays detection-only. |

## Findings

1. **A hostname is not an identity.** dumpling and retina both report `COMPUTERNAME` WIN-VI96OVQI4I6. They are different machines: 20 GB vs 32 GB RAM, and cursor 2026.09.23 vs 2026.09.18. A capability registry keyed by hostname would merge them. This run keys by `runner_id` (`<ssh-alias>-<adapter>`). A later harness should also record a machine fingerprint that the agent cannot set.
2. **Failed auth is caught as not-certified.** The codex and grok runs failed closed with a recorded reason, and they wrote no files. Scope was clean in all ten runs.
3. **Wall time.** Certified runs took 55-130 s of agent time. Cursor on retina, the oldest CLI (09.18), was the slowest at 130 s.

## To reach five of five

- Codex: fix the codex auth on Alpha, or install codex on a VPS, then rerun `--adapter codex`. The user needs to do this; agents don't touch credentials.
- Grok: `grok login --device-code` on dumpling/demand/retina, then rerun `--adapter grok`. The user needs to do this. The adapter is now pinned to grok 1.0.24 (Light's review), so demand's 1.0.13 needs `grok update` first. Until then it reports `cli_flags_unverified` and is never run.
- Copilot: install the CLI and check its flags with `--help`, then write an adapter.
- Cursor on Alpha: `cursor-agent login`, then rerun.

## Re-certification with machine fingerprints (2026-09-26, harness 6b10ae69…, cursor-cloud-agents 25efbe1)

Cursor was re-certified on the three hosts that hold no cloud credentials. Retina was skipped at its session's request: it holds the runcrew deploy gcloud configs, and Cursor runs unsandboxed. Each row below is the `--export` object the hub accepts (runcrew `tests/test_myhero_cert_interop.py` pins these exact exports).

| runner_id | certified | finished_at | machine fingerprint | expires_at |
| --- | --- | --- | --- | --- |
| dumpling-cursor | True | 2026-09-26T00:20:05Z | bec9b0d5b33164cd… | 2026-10-10T00:20:05Z |
| demand-cursor | True | 2026-09-26T00:20:36Z | 4cbad7fc1ffb1766… | 2026-10-10T00:20:36Z |
| light-cursor | True | 2026-09-26T00:20:15Z | e07cb4cf5608ac6a… | 2026-10-10T00:20:15Z |

The three fingerprints differ. The earlier dumpling/retina hostname collision can no longer merge registry rows, because the registry now withdraws a certification when a known fingerprint changes.

## Codex certification (2026-09-26, harness 6b10ae69…, codex-cli 0.153.4)

Codex's Windows sandbox runs only inside an interactive desktop session. From SSH, a service, or an agent's session 0 it fails with `timed out ... connecting runner pipe-in` or `CryptUnprotectData failed`. A scheduled task `CodexDesktopRunner` (logon type Interactive: it runs only while the user is logged on and stores no password) runs `C:\codex-runner\codex_job.py` inside the logged-on session. It accepts only two fixed job types: `cert` (this harness) and `task` (`codex exec --sandbox workspace-write` in a workspace under `C:\cursor-tasks\` or `C:\runner-cert\work\`). The sandbox is never bypassed. Accounts: Retina and Demand use the blueeyes ChatGPT account; the user signed each host in by device code.

| runner_id | certified | agent s |
| --- | --- | --- |
| dumpling-codex | **yes** | 42.1 |
| retina-codex | **yes** | 73.4 |
| light-codex | **yes** | 77.1 |
| demand-codex | **yes** | 104.4 |
| alpha-codex | no | 27.2: `CryptUnprotectData failed` even in the desktop session. The sandbox secrets in `~/.codex/.sandbox-secrets` date from 2026-05-18 and look stale, so they need re-provisioning (the user's decision) |

Finding: retina's MachineGuid fingerprint (4cbad7fc…) equals demand's, although they are different machines. The images were probably cloned, so MachineGuid is not unique across this fleet either, and the runner_id must stay the identity key.
