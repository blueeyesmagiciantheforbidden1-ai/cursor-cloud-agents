import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import request from "supertest";
import { createApp } from "./app.js";
import { TaskStore } from "./store.js";

test("GET /api/health returns ok", async () => {
  const app = createApp();
  const res = await request(app).get("/api/health");
  assert.equal(res.status, 200);
  assert.equal(res.body.status, "ok");
});

test("tasks lifecycle: create, list, toggle, delete", async () => {
  const app = createApp(new TaskStore());

  const created = await request(app).post("/api/tasks").send({ title: "Write tests" });
  assert.equal(created.status, 201);
  assert.equal(created.body.title, "Write tests");
  assert.equal(created.body.done, false);
  const id = created.body.id as string;

  const listed = await request(app).get("/api/tasks");
  assert.equal(listed.status, 200);
  assert.equal(listed.body.length, 1);

  const renamed = await request(app).patch(`/api/tasks/${id}`).send({ title: "Write more tests" });
  assert.equal(renamed.status, 200);
  assert.equal(renamed.body.title, "Write more tests");

  const toggled = await request(app).patch(`/api/tasks/${id}`).send({ done: true });
  assert.equal(toggled.status, 200);
  assert.equal(toggled.body.done, true);

  const removed = await request(app).delete(`/api/tasks/${id}`);
  assert.equal(removed.status, 204);

  const empty = await request(app).get("/api/tasks");
  assert.equal(empty.body.length, 0);
});

test("POST /api/tasks rejects empty title", async () => {
  const app = createApp(new TaskStore());
  const res = await request(app).post("/api/tasks").send({ title: "   " });
  assert.equal(res.status, 400);
});

test("PATCH /api/tasks/:id returns 404 for unknown id", async () => {
  const app = createApp(new TaskStore());
  const res = await request(app)
    .patch("/api/tasks/missing-id")
    .send({ done: true });
  assert.equal(res.status, 404);
  assert.equal(res.body.error, "task not found");
});

test("DELETE /api/tasks/:id returns 404 for unknown id", async () => {
  const app = createApp(new TaskStore());
  const res = await request(app).delete("/api/tasks/missing-id");
  assert.equal(res.status, 404);
  assert.equal(res.body.error, "task not found");
});

test("PATCH /api/tasks/:id rejects blank title", async () => {
  const app = createApp(new TaskStore());
  const created = await request(app).post("/api/tasks").send({ title: "Keep me" });
  assert.equal(created.status, 201);

  const res = await request(app)
    .patch(`/api/tasks/${created.body.id}`)
    .send({ title: "   " });
  assert.equal(res.status, 400);
  assert.equal(res.body.error, "title is required");

  const listed = await request(app).get("/api/tasks");
  assert.equal(listed.body.length, 1);
  assert.equal(listed.body[0].title, "Keep me");
});

test("serves client dist and SPA fallback without swallowing API 404s", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cca-client-"));
  fs.writeFileSync(
    path.join(dir, "index.html"),
    "<!doctype html><title>Task Board UI</title>",
  );
  const app = createApp(new TaskStore(), { clientDist: dir });

  const root = await request(app).get("/");
  assert.equal(root.status, 200);
  assert.match(root.text, /Task Board UI/);

  const spa = await request(app).get("/does-not-exist");
  assert.equal(spa.status, 200);
  assert.match(spa.text, /Task Board UI/);

  const api404 = await request(app).get("/api/nope");
  assert.equal(api404.status, 404);

  const health = await request(app).get("/api/health");
  assert.equal(health.status, 200);
  assert.equal(health.body.status, "ok");
});
