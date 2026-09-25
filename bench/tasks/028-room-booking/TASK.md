We need a small meeting-room booking system split across `rooms/inventory.py`, `rooms/bookings.py`, and `rooms/finder.py`.

Time uses integer minutes and half-open intervals `[start, end)`.

**Inventory** (`RoomInventory`):

- `add_room(room_id: str, capacity: int, equipment: set[str] | None = None)` — capacity must be positive. Duplicate `room_id` raises `ValueError`. Store equipment as a frozenset (empty if omitted).
- `get(room_id)` — return a dict `{'room_id', 'capacity', 'equipment'}` or raise `KeyError`.
- `all_rooms()` — list of those dicts sorted by `room_id`.

**Bookings** (`BookingLedger`):

- Construct with a `RoomInventory`.
- `reserve(room_id, start, end, title='')` — raise `KeyError` if room unknown; `ValueError` if `start >= end` or either negative. If the interval overlaps an existing reservation for that room, raise `rooms.bookings.ConflictError`. On success return a booking id string (unique within the ledger).
- `cancel(booking_id)` — `True` if removed, `False` if unknown.
- `for_room(room_id)` — list of dicts `{'booking_id','room_id','start','end','title'}` sorted by start.

**Finder** (`find_rooms(inventory, ledger, start, end, min_capacity=1, need_equipment=None)`):

- Return room_id strings that are free for `[start, end)`, have capacity >= `min_capacity`, and whose equipment is a superset of `need_equipment` (default empty). Sorted alphabetically.
- Validate the window like `reserve` (`ValueError` on bad window).

Implement the stubs so they work together. Do not mutate caller-provided equipment sets.
