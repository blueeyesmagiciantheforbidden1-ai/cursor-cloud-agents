class ConflictError(Exception):
    pass


def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


class BookingLedger:
    def __init__(self, inventory):
        self._inventory = inventory
        self._bookings = {}
        self._n = 0

    def reserve(self, room_id, start, end, title=''):
        self._inventory.get(room_id)
        if start < 0 or end < 0 or start >= end:
            raise ValueError('invalid interval')
        for existing in self._bookings.values():
            if existing['room_id'] == room_id and _overlaps(
                start, end, existing['start'], existing['end']
            ):
                raise ConflictError('room busy')
        self._n += 1
        bid = f'b{self._n}'
        self._bookings[bid] = {
            'booking_id': bid,
            'room_id': room_id,
            'start': start,
            'end': end,
            'title': title,
        }
        return bid

    def cancel(self, booking_id):
        return self._bookings.pop(booking_id, None) is not None

    def for_room(self, room_id):
        rows = [dict(b) for b in self._bookings.values() if b['room_id'] == room_id]
        rows.sort(key=lambda b: b['start'])
        return rows
