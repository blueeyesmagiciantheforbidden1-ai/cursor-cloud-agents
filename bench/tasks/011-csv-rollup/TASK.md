# CSV-style rollup

I have rows as a list of dicts (like a CSV loaded with DictReader). Please implement a rollup helper.

## API

Package `rollup`, function `summarize(rows, group_key, value_keys)` in `rollup.summarize`
(also re-exported from `rollup`).

- `rows`: list of mapping objects
- `group_key`: string name of the column to group by
- `value_keys`: sequence of column names to sum

Return a **new** list of dicts, one per distinct group value. Each result dict must contain
the group key and every value key (sums). Sort the result by the group value using normal
Python ordering.

## Rules

- Empty `rows` returns `[]`.
- If any row is missing `group_key` or any of `value_keys`, raise `KeyError`.
- Values under `value_keys` must be numbers (`int` or `float`). Anything else raises `TypeError`.
  Treat `bool` as non-numeric (raise `TypeError`).
- Do not mutate the input `rows` or the dicts inside it.
- Sums of ints may stay int; mixing int and float may yield float (normal Python `+` is fine).

There are a few starter tests under `tests/`.
