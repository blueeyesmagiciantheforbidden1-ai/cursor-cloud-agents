"""Merge overlapping intervals — buggy."""


def merge(intervals):
    # Bugs: mutates input order via sort in place; does not merge touching; skips validation
    intervals.sort(key=lambda iv: iv[0])
    if not intervals:
        return []
    out = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start < out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out
