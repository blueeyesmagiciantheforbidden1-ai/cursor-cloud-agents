"""Bounded least-recently-used cache."""

from collections import OrderedDict


class LRUCache:
    def __init__(self, maxsize):
        if not isinstance(maxsize, int) or maxsize < 1:
            raise ValueError('maxsize must be an integer >= 1')
        self._maxsize = maxsize
        self._data = OrderedDict()

    def set(self, key, value):
        if key in self._data:
            self._data.move_to_end(key)
            self._data[key] = value
            return
        if len(self._data) >= self._maxsize:
            self._data.popitem(last=False)
        self._data[key] = value

    def get(self, key, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def delete(self, key):
        if key not in self._data:
            return False
        del self._data[key]
        return True

    def __len__(self):
        return len(self._data)
