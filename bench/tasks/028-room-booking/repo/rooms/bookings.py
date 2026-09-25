class ConflictError(Exception):
    pass


class BookingLedger:
    def __init__(self, inventory):
        self._inventory = inventory
        self._bookings = {}
        self._n = 0

    def reserve(self, room_id, start, end, title=''):
        # BUG: no overlap check; no window validation
        self._inventory.get(room_id)
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
        return [b for b in self._bookings.values() if b['room_id'] == room_id]
