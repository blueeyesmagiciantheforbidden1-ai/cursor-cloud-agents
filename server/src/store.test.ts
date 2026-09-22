import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { TaskStore } from "./store.js";

function tmpTasksFile(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cca-tasks-"));
  return path.join(dir, "tasks.json");
}

test("persists creates, updates, and deletes across store instances", () => {
  const filePath = tmpTasksFile();
  const store = new TaskStore({ filePath });
  const created = store.create("Persist me");
  store.update(created.id, { done: true, title: "Persisted" });

  const reloaded = new TaskStore({ filePath });
  assert.equal(reloaded.list().length, 1);
  assert.equal(reloaded.list()[0].id, created.id);
  assert.equal(reloaded.list()[0].title, "Persisted");
  assert.equal(reloaded.list()[0].done, true);

  reloaded.remove(created.id);
  const empty = new TaskStore({ filePath });
  assert.equal(empty.list().length, 0);
});

test("seed writes defaults once and does not duplicate on reload", () => {
  const filePath = tmpTasksFile();
  const first = new TaskStore({ filePath });
  first.seed();
  assert.equal(first.list().length, 2);

  const second = new TaskStore({ filePath });
  second.seed();
  assert.equal(second.list().length, 2);
  assert.deepEqual(
    second.list().map((task) => task.title).sort(),
    ["Explore the starter API", "Read the README"],
  );
});

test("load skips a missing file and throws on invalid JSON", () => {
  const missing = new TaskStore({ filePath: tmpTasksFile() });
  assert.equal(missing.list().length, 0);

  const filePath = tmpTasksFile();
  fs.writeFileSync(filePath, "{not-json", "utf8");
  assert.throws(() => new TaskStore({ filePath }), SyntaxError);
});
