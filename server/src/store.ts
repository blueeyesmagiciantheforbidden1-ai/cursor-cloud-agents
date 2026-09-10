export interface Task {
  id: string;
  title: string;
  done: boolean;
  createdAt: string;
}

export class TaskStore {
  private tasks: Map<string, Task> = new Map();

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
    return task;
  }

  update(id: string, patch: Partial<Pick<Task, "title" | "done">>): Task | undefined {
    const existing = this.tasks.get(id);
    if (!existing) return undefined;
    const updated: Task = { ...existing, ...patch };
    this.tasks.set(id, updated);
    return updated;
  }

  remove(id: string): boolean {
    return this.tasks.delete(id);
  }

  seed(): void {
    if (this.tasks.size > 0) return;
    this.create("Explore the starter API");
    const done = this.create("Read the README");
    this.update(done.id, { done: true });
  }
}

function cryptoRandomId(): string {
  return globalThis.crypto.randomUUID();
}
