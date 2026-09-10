# cursor-cloud-agents

Starter environment for Cursor Cloud Agents — a full-stack TypeScript monorepo you can run, extend, and demo end to end.

## Stack

- **client/** — React 18 + Vite 6 + TypeScript single-page app (Task Board UI)
- **server/** — Express 4 + TypeScript REST API with an in-memory task store
- **npm workspaces** — one install at the root wires both packages together

## Prerequisites

- Node.js >= 20 (Node 22 recommended)
- npm >= 10

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
