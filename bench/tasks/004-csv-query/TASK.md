# CSV filter and column select

Please add `csvquery.select(csv_text, columns, where=None)` (importable from `csvquery`).

Inputs:

- `csv_text`: a CSV string with a header row (use the stdlib `csv` module)
- `columns`: a list of column names to keep, in that order
- `where`: optional `(column_name, value)` pair meaning keep rows where that column equals `value` as a string; if `where` is `None`, keep all rows

Behavior:

- Return CSV text with a header of exactly `columns`, then the matching data rows.
- If a requested column is missing from the input header, raise `KeyError`.
- If `where` names a missing column, raise `KeyError`.
- Preserve field quoting rules via the csv module (do not hand-roll escaping).
- An input with only a header (no data rows) still yields a header-only result.

The package currently only has a placeholder; wire it up and implement this.
