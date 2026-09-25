class Slot:
    def __init__(self, start, end):
        # BUG: no validation; uses inclusive end mentally elsewhere
        self.start = start
        self.end = end

    @property
    def duration(self):
        return self.end - self.start

    def overlaps(self, other):
        # BUG: closed-interval overlap (treats touching ends as overlap)
        return self.start <= other.end and other.start <= self.end
