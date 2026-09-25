class RoomInventory:
    def __init__(self):
        self._rooms = {}

    def add_room(self, room_id, capacity, equipment=None):
        # BUG: no validation; mutates equipment set; allows duplicates overwrite
        self._rooms[room_id] = {
            'room_id': room_id,
            'capacity': capacity,
            'equipment': equipment if equipment is not None else set(),
        }

    def get(self, room_id):
        return self._rooms[room_id]

    def all_rooms(self):
        return list(self._rooms.values())
