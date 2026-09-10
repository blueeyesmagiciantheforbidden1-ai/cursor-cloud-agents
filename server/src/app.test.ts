import { test } from "node:test";
import assert from "node:assert/strict";
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
