"""Overlap check — buggy."""


def overlaps(a, b):
    # Bug: treats touching half-open intervals as overlapping; weak validation
    return a[0] <= b[1] and b[0] <= a[1]
