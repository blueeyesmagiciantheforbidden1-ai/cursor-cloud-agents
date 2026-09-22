import fs from "node:fs";
import path from "node:path";
import type { Task, UpdateTaskBody } from "@cursor-cloud-agents/types";

export type { Task };

export type TaskStoreOptions = {
  filePath?: string;
};

export class TaskStore {
  private tasks: Map<string, Task> = new Map();
  private readonly filePath: string | undefined;

  constructor(options: TaskStoreOptions = {}) {
    this.filePath = options.filePath;
    this.load();
  }

  list(): Task[] {
    return [...this.tasks.values()].sort((a, b) =>
      a.createdAt.localeCompare(b.createdAt),
    );
  }

  create(title: string): Task {
    const task: Task = {
      id: cryptoRandomId(),
      title,
      done: false,
      createdAt: new Date().toISOString(),
    };
    this.tasks.set(task.id, task);
    this.persist();
    return task;
  }

  update(id: string, patch: UpdateTaskBody): Task | undefined {
    const existing = this.tasks.get(id);
    if (!existing) return undefined;
    const updated: Task = { ...existing, ...patch };
    this.tasks.set(id, updated);
    this.persist();
    return updated;
  }

  remove(id: string): boolean {
    const deleted = this.tasks.delete(id);
    if (deleted) this.persist();
    return deleted;
  }

  seed(): void {
    if (this.tasks.size > 0) return;
    this.create("Explore the starter API");
    const done = this.create("Read the README");
    this.update(done.id, { done: true });
  }

  private load(): void {
    if (!this.filePath) return;
    let raw: string;
    try {
      raw = fs.readFileSync(this.filePath, "utf8");
    } catch (err) {
      if (isEnoent(err)) return;
      throw err;
    }
    const trimmed = raw.trim();
    if (!trimmed) return;
    const parsed: unknown = JSON.parse(trimmed);
    if (!Array.isArray(parsed)) {
      throw new Error(`Task store file must contain a JSON array: ${this.filePath}`);
    }
    for (const entry of parsed) {
      if (!isTask(entry)) {
        throw new Error(`Task store file contains an invalid task: ${this.filePath}`);
      }
      this.tasks.set(entry.id, entry);
    }
  }

  private persist(): void {
    if (!this.filePath) return;
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
    const tmpPath = `${this.filePath}.${process.pid}.tmp`;
    fs.writeFileSync(tmpPath, `${JSON.stringify(this.list(), null, 2)}\n`, "utf8");
    try {
      fs.renameSync(tmpPath, this.filePath);
    } catch {
      if (fs.existsSync(this.filePath)) fs.unlinkSync(this.filePath);
      fs.renameSync(tmpPath, this.filePath);
    }
  }
}

function cryptoRandomId(): string {
  return globalThis.crypto.randomUUID();
}

function isEnoent(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    "code" in err &&
    (err as { code: unknown }).code === "ENOENT"
  );
}

function isTask(value: unknown): value is Task {
  if (typeof value !== "object" || value === null) return false;
  const task = value as Record<string, unknown>;
  return (
    typeof task.id === "string" &&
    typeof task.title === "string" &&
    typeof task.done === "boolean" &&
    typeof task.createdAt === "string"
  );
}
