"""Numeric clamp helper."""


def clamp(value, low, high):
    """Return value restricted to the inclusive range [low, high].

    Raises ValueError if low > high.
    """
    if low > high:
        raise ValueError('low must not exceed high')
    if value < low:
        return low
    if value > high:
        return high
    return value
