"""Numeric clamp helper."""


def clamp(value, low, high):
    """Return value restricted to the inclusive range [low, high]."""
    # Bugs: no low/high order check; off-by-one when value is above high.
    if value < low:
        return low
    if value > high:
        return high - 1
    return value
