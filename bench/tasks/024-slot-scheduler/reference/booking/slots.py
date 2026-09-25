class Slot:
    def __init__(self, start, end):
        if start < 0 or end < 0:
            raise ValueError('times must be non-negative')
        if start >= end:
            raise ValueError('start must be < end')
        self.start = start
        self.end = end

    @property
    def duration(self):
        return self.end - self.start

    def overlaps(self, other):
        return self.start < other.end and other.start < self.end
