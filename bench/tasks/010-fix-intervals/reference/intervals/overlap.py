"""Half-open interval overlap."""


def _check_interval(iv):
    try:
        start, end = iv
    except (TypeError, ValueError):
        raise TypeError('interval must be a pair') from None
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        raise TypeError('interval bounds must be numbers')
    if isinstance(start, bool) or isinstance(end, bool):
        raise TypeError('interval bounds must be numbers')
    if start > end:
        raise ValueError('start must be <= end')
    return start, end


def overlaps(a, b):
    a0, a1 = _check_interval(a)
    b0, b1 = _check_interval(b)
    return a0 < b1 and b0 < a1
