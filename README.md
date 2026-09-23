# cursor-cloud-agents

Starter environment for Cursor Cloud Agents — a full-stack TypeScript monorepo you can run, extend, and demo end to end.

## Hub pack

`agent-hub/`, `live-worker-runtime/`, `hub.py`, and the live-fleet helper scripts are the RunCrew hub source imported from Alpha's local pack at `C:\API_KEYS`. Live API keys, tokens, and `.env` files are not in this tree. Owner emails, the billing account id, and live ChatGPT connector ids in the public copy are placeholders. See `STATUS.md` for what landed and the next steps for Light, Retina, and Demand.

## Stack

- **client/** — React 18 + Vite 6 + TypeScript single-page app (Task Board UI)
- **server/** — Express 4 + TypeScript REST API with an in-memory task store
- **npm workspaces** — one install at the root wires both packages together

## Prerequisites

- Node.js >= 20 (Node 22 recommended)
- npm >= 10

## Windows self-hosted worker: better-sqlite3 ABI repair

Cursor’s official Windows `agent-cli` bundles (both **x64** and **arm64**) currently
ship Node.js 24 (`NODE_MODULE_VERSION` **137**) with a `better-sqlite3` native
binary built for Node.js 22 (`NODE_MODULE_VERSION` **127**). That mismatch
crashes `exec-daemon` right after the worker authenticates.

Repair **both** architectures in place without clearing sign-in (the script only
replaces `better_sqlite3.node` and never deletes `%LOCALAPPDATA%\cursor-agent`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\repair-cursor-agent-sqlite.ps1
agent worker start
```

Dry run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\repair-cursor-agent-sqlite.ps1 -DryRun
```

Verify against clean official Windows packages (CI / Linux hosts):

```bash
./scripts/verify-repair-cursor-agent-sqlite.sh
```

Note: `node_sqlite3.node` is a separate N-API module. On some Windows hosts Smart
App Control may still block it; that is unrelated to this ABI repair. WSL remains
the supported workaround when Code Integrity blocks unsigned native modules.

## Getting started

```bash
npm ci        # install all workspace dependencies
npm run dev   # start API (http://localhost:3001) + client (http://localhost:5173)
```

The Vite dev server proxies `/api/*` to the Express server, so open
http://localhost:5173 and start adding tasks.

## Useful commands

| Command | Description |
| --- | --- |
| `npm run dev` | Run API and client together (watch mode) |
| `npm run build` | Type-check + build server and client for production |
| `npm run typecheck` | Type-check both workspaces |
| `npm run lint` | Lint the whole repo with ESLint |
| `npm test` | Run the server API test suite |

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | Health probe |
| GET | `/api/tasks` | List tasks |
| POST | `/api/tasks` | Create a task (`{ "title": "…" }`) |
| PATCH | `/api/tasks/:id` | Update `title` and/or `done` |
| DELETE | `/api/tasks/:id` | Delete a task |
