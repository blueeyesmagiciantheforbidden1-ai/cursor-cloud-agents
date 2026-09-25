# Implement a small LRU cache

Please implement `lrucache.LRUCache` with this API:

- `LRUCache(maxsize)` — `maxsize` must be an integer `>= 1`, otherwise raise `ValueError`
- `set(key, value)` — insert or update; on insert when already full, evict the least recently used key first
- `get(key, default=None)` — return the value, or `default` if missing; a successful get counts as use (moves to most-recently-used)
- `delete(key)` — remove key if present; return `True` if removed, `False` if it was absent
- `len(cache)` — number of stored entries
- updating an existing key with `set` also counts as a use

Do not mutate caller-provided containers beyond storing the reference as the value. Stdlib only; no third-party deps.

Export `LRUCache` from the `lrucache` package.
