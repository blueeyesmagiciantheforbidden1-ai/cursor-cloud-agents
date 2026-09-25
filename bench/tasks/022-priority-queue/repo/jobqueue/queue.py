"""Broken stub: always LIFO, ignores priority, no duplicate check."""


class PriorityQueue:
    def __init__(self):
        self._items = []

    def push(self, job):
        self._items.append(job)

    def pop(self):
        if not self._items:
            raise IndexError('empty')
        return self._items.pop()

    def peek(self):
        if not self._items:
            raise IndexError('empty')
        return self._items[-1]

    def cancel(self, job_id):
        for i, job in enumerate(self._items):
            if job.job_id == job_id:
                del self._items[i]
                return True
        return False

    def __len__(self):
        return len(self._items)
