import fs from "node:fs";
import path from "node:path";
import express, { type Express, type Request, type Response } from "express";
import cors from "cors";
import type { UpdateTaskBody } from "@cursor-cloud-agents/types";
import { TaskStore } from "./store.js";

export type CreateAppOptions = {
  clientDist?: string;
};

export function createApp(
  store: TaskStore = new TaskStore(),
  options: CreateAppOptions = {},
): Express {
  const app = express();
  app.use(cors());
  app.use(express.json());

  app.get("/api/health", (_req: Request, res: Response) => {
    res.json({ status: "ok", uptime: process.uptime() });
  });

  app.get("/api/tasks", (_req: Request, res: Response) => {
    res.json(store.list());
  });

  app.post("/api/tasks", (req: Request, res: Response) => {
    const title = typeof req.body?.title === "string" ? req.body.title.trim() : "";
    if (!title) {
      return res.status(400).json({ error: "title is required" });
    }
    const task = store.create(title);
    res.status(201).json(task);
  });

  app.patch("/api/tasks/:id", (req: Request, res: Response) => {
    const patch: UpdateTaskBody = {};
    if (typeof req.body?.title === "string") {
      const title = req.body.title.trim();
      if (!title) {
        return res.status(400).json({ error: "title is required" });
      }
      patch.title = title;
    }
    if (typeof req.body?.done === "boolean") patch.done = req.body.done;
    const task = store.update(req.params.id, patch);
    if (!task) return res.status(404).json({ error: "task not found" });
    res.json(task);
  });

  app.delete("/api/tasks/:id", (req: Request, res: Response) => {
    const removed = store.remove(req.params.id);
    if (!removed) return res.status(404).json({ error: "task not found" });
    res.status(204).end();
  });

  const indexHtml = options.clientDist
    ? path.join(options.clientDist, "index.html")
    : null;
  if (indexHtml && options.clientDist && fs.existsSync(indexHtml)) {
    const clientDist = options.clientDist;
    app.use(express.static(clientDist));
    app.use((req, res, next) => {
      if (req.method !== "GET" && req.method !== "HEAD") return next();
      if (req.path.startsWith("/api")) return next();
      res.sendFile(indexHtml, (err) => {
        if (err) next(err);
      });
    });
  }

  return app;
}
