Build a simple appointment calendar in the `booking` package.

Time is integer minutes from an arbitrary origin (e.g. minutes since midnight). Intervals are half-open: `[start, end)` — an appointment ending at T does not conflict with one starting at T.

`booking/slots.py`:

- `Slot(start: int, end: int)` — raise `ValueError` if `start >= end` or either is negative.
- `overlaps(other) -> bool` — true iff the half-open intervals overlap.
- `duration` property — `end - start`.

`booking/calendar.py`:

- `Calendar` stores non-overlapping booked slots.
- `book(slot)` — add the slot if it does not overlap any existing booking; return `True`. If it overlaps, leave the calendar unchanged and return `False`.
- `cancel(start, end)` — remove the exact slot with that start and end if present; return `True`/`False`.
- `available(window_start, window_end, duration)` — return a list of `Slot` objects of the given `duration` that fit entirely inside `[window_start, window_end)` and do not overlap existing bookings. Scan from `window_start` upward in steps of 1 minute; include every valid start. Raise `ValueError` if `duration <= 0` or the window is invalid (`window_start >= window_end` or negative bounds).
- `bookings()` — return booked slots sorted by start time (a new list).

Do not mutate slots that callers pass in.
