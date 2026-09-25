from .slots import Slot


class Calendar:
    def __init__(self):
        self._booked = []

    def book(self, slot):
        # BUG: always appends even on overlap
        self._booked.append(slot)
        return True

    def cancel(self, start, end):
        for i, s in enumerate(self._booked):
            if s.start == start and s.end == end:
                del self._booked[i]
                return True
        return False

    def available(self, window_start, window_end, duration):
        # BUG: naive — ignores bookings and does not validate
        result = []
        t = window_start
        while t + duration <= window_end:
            result.append(Slot(t, t + duration))
            t += 1
        return result

    def bookings(self):
        return self._booked
