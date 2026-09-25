def find_rooms(inventory, ledger, start, end, min_capacity=1, need_equipment=None):
    if start < 0 or end < 0 or start >= end:
        raise ValueError('invalid interval')
    need = frozenset(need_equipment or ())
    out = []
    for room in inventory.all_rooms():
        if room['capacity'] < min_capacity:
            continue
        if not need.issubset(room['equipment']):
            continue
        busy = False
        for b in ledger.for_room(room['room_id']):
            if start < b['end'] and b['start'] < end:
                busy = True
                break
        if not busy:
            out.append(room['room_id'])
    return sorted(out)
