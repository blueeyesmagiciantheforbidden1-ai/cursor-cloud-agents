Please review and fix `ratelimit/bucket.py`.

`TokenBucket(capacity: int, refill_rate: float)` models a classic token bucket:

- `capacity` is the maximum tokens (positive int). `refill_rate` is tokens added per second (positive float).
- The bucket starts full (`capacity` tokens).
- `allow(tokens: int = 1, now: float | None = None) -> bool` — if `now` is omitted, use `time.monotonic()`. Refill based on elapsed time since the last update: `tokens += elapsed * refill_rate`, then clamp to `capacity`. If the bucket has at least `tokens`, deduct them and return `True`; otherwise return `False` without deducting.
- Raise `ValueError` for non-positive `capacity`, `refill_rate`, or `tokens` argument to `allow`.
- Refill must not exceed capacity. Requesting more tokens than capacity always fails (returns `False`) without changing the balance after refill.

Visible tests only cover a trivial case. Find the logic bugs.
