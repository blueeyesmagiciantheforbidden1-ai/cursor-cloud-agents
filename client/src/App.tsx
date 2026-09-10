import { useEffect, useMemo, useState } from "react";
import { api, type Task } from "./api";

export function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [online, setOnline] = useState(false);

  useEffect(() => {
    void refresh();
    void api
      .health()
      .then(() => setOnline(true))
      .catch(() => setOnline(false));
  }, []);

  async function refresh() {
    try {
      setTasks(await api.list());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function addTask(event: React.FormEvent) {
    event.preventDefault();
    const value = title.trim();
    if (!value) return;
    try {
      const task = await api.create(value);
      setTasks((prev) => [...prev, task]);
      setTitle("");
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function toggleTask(task: Task) {
    try {
      const updated = await api.toggle(task.id, !task.done);
      setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function removeTask(task: Task) {
    try {
      await api.remove(task.id);
      setTasks((prev) => prev.filter((t) => t.id !== task.id));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const remaining = useMemo(() => tasks.filter((t) => !t.done).length, [tasks]);

  return (
    <div className="page">
      <main className="card">
        <header className="card__header">
          <div className="brand">
            <span className="brand__dot" />
            <h1>Task Board</h1>
          </div>
          <span className={`status ${online ? "status--ok" : "status--down"}`}>
            {online ? "API online" : "API offline"}
          </span>
        </header>

        <p className="subtitle">
          A Cursor Cloud Agents starter — React + Vite talking to an Express API.
        </p>

        <form className="composer" onSubmit={addTask}>
          <input
            className="composer__input"
            placeholder="Add a task and press Enter…"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            aria-label="New task title"
          />
          <button className="composer__button" type="submit">
            Add
          </button>
        </form>

        {error && <div className="error">{error}</div>}

        {loading ? (
          <p className="muted">Loading tasks…</p>
        ) : tasks.length === 0 ? (
          <p className="muted">No tasks yet. Add your first one above.</p>
        ) : (
          <ul className="list">
            {tasks.map((task) => (
              <li key={task.id} className={`item ${task.done ? "item--done" : ""}`}>
                <label className="item__label">
                  <input
                    type="checkbox"
                    checked={task.done}
                    onChange={() => toggleTask(task)}
                  />
                  <span>{task.title}</span>
                </label>
                <button
                  className="item__delete"
                  onClick={() => removeTask(task)}
                  aria-label={`Delete ${task.title}`}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}

        <footer className="card__footer">
          <span>{remaining} remaining</span>
          <span>{tasks.length} total</span>
        </footer>
      </main>
    </div>
  );
}
