Implement a tiny CSV join helper in `csvtools`.

`csvtools/reader.py`:

- `read_rows(text: str) -> list[dict]` — parse CSV text with a header row into a list of dicts (string values). Use the stdlib `csv` module. If the text is empty or only whitespace, return `[]`. Header-only input (no data rows) also returns `[]`.

`csvtools/join.py`:

- `join_rows(left, right, key, how='inner')` — `left` and `right` are lists of dicts. Join on column `key`.
  - `how='inner'`: only rows whose key appears in both sides.
  - `how='left'`: all left rows; missing right columns filled with `None`.
  - If a key value appears multiple times on the right, produce one output row per match (cartesian on that key).
  - Output dicts: left columns first (as in the left row), then right columns except `key` (skip duplicate key). If a non-key column name exists on both sides, prefix the right one with `right_`.
  - Raise `ValueError` if `how` is not `inner` or `left`, or if `key` is missing from either side's rows when that side is non-empty.
  - Do not mutate the input lists or dicts.

Wire exports in `__init__.py` if needed.
