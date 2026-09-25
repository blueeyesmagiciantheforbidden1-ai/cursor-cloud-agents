class LRUCache:
    def __init__(self, capacity):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError('capacity must be positive int')
        self.capacity = capacity
        self._data = {}
        self._order = []

    def get(self, key):
        if key not in self._data:
            return None
        self._order.remove(key)
        self._order.append(key)
        return self._data[key]

    def put(self, key, value):
        if key in self._data:
            self._data[key] = value
            self._order.remove(key)
            self._order.append(key)
            return
        if len(self._data) >= self.capacity:
            victim = self._order.pop(0)
            del self._data[victim]
        self._data[key] = value
        self._order.append(key)

    def __len__(self):
        return len(self._data)
