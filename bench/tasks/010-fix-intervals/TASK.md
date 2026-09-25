# Interval helpers are wrong at the edges

Package `intervals` exposes two functions (importable from the package root):

1. `merge(intervals)` — `intervals` is a list of half-open `[start, end)` pairs (`end` may equal `start` for empty). Return a new list of merged half-open intervals sorted by start. Touching intervals like `[1, 2)` and `[2, 3)` must merge into `[1, 3)`. Do not mutate the input list or the pair objects inside it.

2. `overlaps(a, b)` — each of `a` and `b` is a half-open `[start, end)` pair. Return `True` if they share any point on the number line under half-open rules. Touching endpoints only (e.g. `[1, 2)` and `[2, 3)`) do **not** overlap.

Both should raise `TypeError` if an interval is not a 2-sequence of numbers, and `ValueError` if `start > end`.

Please fix the bugs — visible tests only cover a couple of obvious cases.
