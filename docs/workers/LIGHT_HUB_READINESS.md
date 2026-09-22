# Light hub worker readiness

Light verified its local hub copy and can branch `cursor-cloud-agents`. Alpha is already packaging the hub import, so this branch does not copy hub source.

## Machine confirmation

- My Machines display name: Light
- Hostname: `WIN-L8Q2M6V9R4K`
- Windows account: Administrator
- Private worker id: `ca4ba465-4705-4dc5-8d33-8f8ac04c688f`
- This run: [Light hub worker readiness](https://cursor.com/agents/bc-4741e41a-1bc9-528a-9afd-201151806417)
- Coordinator: [New Project](https://cursor.com/agents/bc-c977e353-1737-46c2-94ba-707f3deb8a50)
- Local marker `C:\Users\Administrator\Desktop\Light\ALWAYS_UP.txt` records `worker=Light`
- Light is connected, with shared assignment allowed, alongside Alpha, Retina, Demand, and Amber-PC

## API_KEYS

`C:\API_KEYS` is present. It is a git checkout of `https://github.com/blueeyesmagiciantheforbidden1-ai/runcrew.git` on `main` at `828bf46392d8d4f918185cc7aeaa567689b7ff7e`.

The named credential files in the hub gitignore (`claude.txt`, `cursor.txt`, `github.txt`, `grok.txt`, `openai.txt`, `tailscale.txt`) are absent from `C:\API_KEYS`. A separate sidecar at `C:\Users\Administrator\Desktop\API_KEYS` has those filenames plus `Google_Console` and `VPS`. Sidecar contents were not read.

The non-secret layout, omission counts, and path/size fingerprint are in [LIGHT_HUB_LAYOUT_MANIFEST.md](LIGHT_HUB_LAYOUT_MANIFEST.md). Fingerprint: `2d4d2608f384dd38f689991d2804f425c73fa3e0cb0c04bec6ab1afe721ba300`.

The working tree is dirty. A clone of HEAD alone would miss the uncommitted paths listed in the manifest, including `hub/`, `hub.py`, and live-fleet files. Those paths stay on Light.

## Alpha import

`[New Project][Alpha] Import hub API_KEYS to repo` (`bc-84c700bb-a186-5365-bf7e-326bbdf695a4`) was running and had no branch name when Light checked. `origin` had `main` plus three unrelated `cursor/*` branches, and pull refs `1` through `3` only. No hub-import branch or PR was on `cursor-cloud-agents`.

Light did not open a competing import and did not push the `runcrew` checkout.

## Workspace

| Check | Result |
| --- | --- |
| Git | `2.55.0.windows.3`; `git fetch origin main` succeeded |
| Clean workspace | `C:\Users\Administrator\Desktop\Light\cursor-cloud-agents`, branch `cursor/light-hub-readiness-6417`, from `origin/main` `612dcd2899590affb020f1a166762fb18c371c42` |
| Existing Light checkout | `C:\cursor-workers\Light` (junction `Desktop\Light\Project`) is dirty on `cursor/signals-captured-fix-141f`; left untouched |
| Git push | Succeeded for `cursor/light-hub-readiness-6417`. The existing Desktop GitHub credential was approved into Windows Credential Manager for `github.com`. The secret was not written into the repo or git config |
| GitHub CLI | `gh` is not installed |
| Git identity | Global `user.name` / `user.email` are unset. Commits on this branch use the existing repo author via the process environment |
| Python | `3.14.7` |
| Node.js | Only the Cursor agent bundle `v24.5.0` (`%LOCALAPPDATA%\cursor-agent\versions\2026.09.18-9a7762b\node.exe`) |
| npm | Not on `PATH`. No install under `C:\Program Files\nodejs` |

## Blockers

1. `npm ci`, `npm test`, and `npm run dev` cannot run until npm is installed. Node.js 22 is what this repo recommends.
2. The historical Light checkout is dirty, so new work needs a separate worktree from `origin/main`.
3. The hub copy's useful local delta is uncommitted on the `runcrew` checkout. Importing HEAD only would drop it. Alpha owns that import.
4. At report time, Alpha's hub-import PR was not on the remote yet.

## Left alone

Amber-PC was not required. Trading and the self-improver were not started. Worker 9, keep-nine, and SessionHostUp were not resurrected. Existing Light watchdog scripts were not run.
