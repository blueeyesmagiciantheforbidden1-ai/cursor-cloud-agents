class LRUCache:
    def __init__(self, capacity):
        # BUG: no validation
        self.capacity = capacity
        self._data = {}
        self._order = []

    def get(self, key):
        # BUG: does not update recency
        return self._data.get(key)

    def put(self, key, value):
        # BUG: evicts most recent; does not refresh on update; can exceed capacity
        self._data[key] = value
        self._order.append(key)
        if len(self._data) > self.capacity:
            # evict last inserted among order list incorrectly
            victim = self._order[-2] if len(self._order) > 1 else self._order[0]
            self._data.pop(victim, None)

    def __len__(self):
        return len(self._data)
