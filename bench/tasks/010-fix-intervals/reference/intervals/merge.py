"""Merge overlapping and touching half-open intervals."""

from intervals.overlap import _check_interval


def merge(intervals):
    if not isinstance(intervals, list):
        raise TypeError('intervals must be a list')
    normalized = []
    for iv in intervals:
        start, end = _check_interval(iv)
        normalized.append([start, end])
    normalized.sort(key=lambda pair: pair[0])
    if not normalized:
        return []
    out = [normalized[0][:]]
    for start, end in normalized[1:]:
        if start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out
