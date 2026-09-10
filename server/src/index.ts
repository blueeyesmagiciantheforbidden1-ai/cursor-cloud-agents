import { createApp } from "./app.js";
import { TaskStore } from "./store.js";

const PORT = Number(process.env.PORT ?? 3001);

const store = new TaskStore();
store.seed();

const app = createApp(store);

app.listen(PORT, () => {
  console.log(`[server] API listening on http://localhost:${PORT}`);
});
