We need a small in-process priority job queue. The package is `jobqueue`.

Requirements:

1. `Job(job_id: str, priority: int, payload=None)` — lower `priority` number means higher urgency (0 runs before 10). `payload` is opaque. Raise `ValueError` if `job_id` is empty or `priority` is negative.

2. `PriorityQueue` in `jobqueue/queue.py`:
   - `push(job)` — add a job. If a job with the same `job_id` already exists (and has not been popped), raise `ValueError`.
   - `pop()` — remove and return the next job. Raise `IndexError` if empty.
   - `peek()` — return the next job without removing it. Raise `IndexError` if empty.
   - `cancel(job_id)` — remove that job if present; return `True` if removed, `False` if not found.
   - `현실()` — number of pending jobs.
   - When two jobs share the same priority, the one pushed earlier must come out first (FIFO among equals).

Visible tests are incomplete stubs. Implement the real behaviour across the package files as needed.
