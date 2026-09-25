import heapq
import itertools


class PriorityQueue:
    def __init__(self):
        self._heap = []
        self._seq = itertools.count()
        self._entries = {}

    def push(self, job):
        if job.job_id in self._entries:
            raise ValueError('duplicate job_id')
        entry = [job.priority, next(self._seq), job]
        self._entries[job.job_id] = entry
        heapq.heappush(self._heap, entry)

    def _clean(self):
        while self._heap and self._heap[0][2] is None:
            heapq.heappop(self._heap)

    def pop(self):
        self._clean()
        if not self._heap:
            raise IndexError('empty')
        _, _, job = heapq.heappop(self._heap)
        del self._entries[job.job_id]
        return job

    def peek(self):
        self._clean()
        if not self._heap:
            raise IndexError('empty')
        return self._heap[0][2]

    def cancel(self, job_id):
        entry = self._entries.pop(job_id, None)
        if entry is None:
            return False
        entry[2] = None
        return True

    def __len__(self):
        return len(self._entries)
