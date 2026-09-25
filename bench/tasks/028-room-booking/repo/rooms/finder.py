def find_rooms(inventory, ledger, start, end, min_capacity=1, need_equipment=None):
    # BUG: ignores bookings and equipment; unsorted
    need = need_equipment or set()
    out = []
    for room in inventory.all_rooms():
        if room['capacity'] >= min_capacity:
            out.append(room['room_id'])
    return out
