"""Parse compact duration strings."""

import re

_UNIT_SECONDS = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
_ORDER = {'d': 0, 'h': 1, 'm': 2, 's': 3}


def parse_duration(text):
    """Return total seconds for a duration like '1h30m'."""
    if not isinstance(text, str):
        raise ValueError('duration must be a string')
    text = text.strip()
    if not text:
        raise ValueError('empty duration')
    matches = re.findall(r'(\d+)([dhms])', text)
    if not matches:
        raise ValueError('invalid duration')
    rebuilt = ''.join(num + unit for num, unit in matches)
    if rebuilt != text:
        raise ValueError('invalid duration')
    seen = set()
    last_order = -1
    total = 0
    for num, unit in matches:
        if unit in seen:
            raise ValueError('duplicate unit')
        order = _ORDER[unit]
        if order <= last_order:
            raise ValueError('units out of order')
        seen.add(unit)
        last_order = order
        total += int(num) * _UNIT_SECONDS[unit]
    return total
