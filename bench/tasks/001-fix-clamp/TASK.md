# Fix clamp bounds

Our tiny `clampkit` helper is supposed to keep a number inside an inclusive range `[low, high]`.

Right now the visible tests pass for the happy path, but callers are hitting weird results at the edges and when someone passes a backwards range.

Please make `clamp(value, low, high)`:

- return `low` when `value < low`
- return `high` when `value > high`
- return `value` when it is already inside the range (including exactly `low` or `high`)
- raise `ValueError` if `low > high`

Do not mutate anything; just return the clamped number.
