import type { HealthResponse, Task } from "@cursor-cloud-agents/types";

export type { Task };

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { error?: string };
    throw new Error(body.error ?? `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  async health(): Promise<HealthResponse> {
    return json(await fetch("/api/health"));
  },
  async list(): Promise<Task[]> {
    return json(await fetch("/api/tasks"));
  },
  async create(title: string): Promise<Task> {
    return json(
      await fetch("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      }),
    );
  },
  async toggle(id: string, done: boolean): Promise<Task> {
    return json(
      await fetch(`/api/tasks/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ done }),
      }),
    );
  },
  async remove(id: string): Promise<void> {
    const res = await fetch(`/api/tasks/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`Delete failed (${res.status})`);
  },
};
