from .slots import Slot


class Calendar:
    def __init__(self):
        self._booked = []

    def book(self, slot):
        for existing in self._booked:
            if existing.overlaps(slot):
                return False
        self._booked.append(Slot(slot.start, slot.end))
        return True

    def cancel(self, start, end):
        for i, s in enumerate(self._booked):
            if s.start == start and s.end == end:
                del self._booked[i]
                return True
        return False

    def available(self, window_start, window_end, duration):
        if duration <= 0:
            raise ValueError('duration must be positive')
        if window_start < 0 or window_end < 0 or window_start >= window_end:
            raise ValueError('invalid window')
        result = []
        t = window_start
        while t + duration <= window_end:
            candidate = Slot(t, t + duration)
            if all(not b.overlaps(candidate) for b in self._booked):
                result.append(candidate)
            t += 1
        return result

    def bookings(self):
        return sorted((Slot(s.start, s.end) for s in self._booked), key=lambda s: s.start)
