import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createApp } from "./app.js";
import { TaskStore } from "./store.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "../..");
const PORT = Number(process.env.PORT ?? 3001);
const tasksFile =
  process.env.TASKS_FILE ?? path.join(repoRoot, "data", "tasks.json");
const clientDist =
  process.env.CLIENT_DIST ?? path.join(repoRoot, "client", "dist");

const store = new TaskStore({ filePath: tasksFile });
store.seed();

const servingUi = fs.existsSync(path.join(clientDist, "index.html"));
const app = createApp(store, {
  clientDist: servingUi ? clientDist : undefined,
});

app.listen(PORT, () => {
  console.log(`[server] API listening on http://localhost:${PORT}`);
  if (servingUi) {
    console.log(`[server] UI served from ${clientDist}`);
  }
});
