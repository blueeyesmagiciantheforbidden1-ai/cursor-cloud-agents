"""Parse compact duration strings."""

import re

_UNIT_SECONDS = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}


def parse_duration(text):
    """Return total seconds for a duration like '1h30m'. Buggy implementation."""
    text = text.strip()
    # Bug: accepts bare numbers; ignores unit order; fails on 0s
    total = 0
    for num, unit in re.findall(r'(\d+)([dhms])', text):
        total += int(num) * _UNIT_SECONDS[unit]
    if re.match(r'^\d+$', text):
        return int(text)
    return total
