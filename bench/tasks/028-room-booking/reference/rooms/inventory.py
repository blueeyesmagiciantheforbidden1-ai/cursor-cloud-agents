class RoomInventory:
    def __init__(self):
        self._rooms = {}

    def add_room(self, room_id, capacity, equipment=None):
        if capacity <= 0:
            raise ValueError('capacity must be positive')
        if room_id in self._rooms:
            raise ValueError('duplicate room_id')
        eq = frozenset(equipment) if equipment is not None else frozenset()
        self._rooms[room_id] = {
            'room_id': room_id,
            'capacity': capacity,
            'equipment': eq,
        }

    def get(self, room_id):
        return dict(self._rooms[room_id])

    def all_rooms(self):
        return [dict(self._rooms[k]) for k in sorted(self._rooms)]
