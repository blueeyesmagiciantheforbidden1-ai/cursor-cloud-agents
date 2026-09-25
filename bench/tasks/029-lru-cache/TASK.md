Review `cache/lru.py` and fix the LRU cache.

`LRUCache(capacity: int)`:

- `capacity` must be a positive int or raise `ValueError`.
- `get(key)` — return the value if present and mark the key as most-recently used; return `None` if missing (do not raise).
- `put(key, value)` — insert or update; mark as most-recently used. When adding a *new* key would exceed capacity, evict the least-recently used key first.
- `len(cache)` — number of stored entries.
- Updating an existing key with `put` must not change capacity usage beyond replacing the value, and must refresh recency.

The current implementation has several defects around eviction order and `get` behaviour.
