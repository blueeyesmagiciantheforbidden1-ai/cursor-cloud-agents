# TSV join

Join two tab-separated tables on a key column.

## `tsvjoin.table`

- `load_tsv(text: str) -> tuple[list[str], list[dict[str, str]]]`
  First non-empty line is the header (tab-separated column names). Each later
  non-empty line becomes a dict. Rows may have fewer fields than the header;
  missing cells become `''`. Extra fields on a row are ignored.
- `dump_tsv(headers: list[str], rows: list[dict]) -> str`
  Header line plus one line per row. Missing keys become `''`. Use `\n` newlines.
  No trailing newline after the last line if there are no rows? Always end the
  text with a single trailing newline if there is at least a header.

## `tsvjoin.join`

- `join_tables(left_headers, left_rows, right_headers, right_rows, key: str, how: str) -> tuple[list[str], list[dict]]`
  - `how` is `'inner'` or `'left'` (anything else -> `ValueError`).
  - Output headers: all left headers, then right headers except `key` (if `key`
    appears on the right). If a non-key column name exists on both sides, suffix
    the right-hand column with `_right`.
  - Matching is on string equality of the key field.
  - Inner: only left rows with at least one match.
  - Left: every left row; if no match, fill right columns with `''`.
  - If multiple right rows match, emit one output row per match (for that left row).
  - If `key` missing from left headers, raise `KeyError`.
  - Do not mutate input row dicts.

## CLI (`python -m tsvjoin`)

Args: `LEFT.tsv RIGHT.tsv --key KEY [--how inner|left]` (default how=inner).
Read both files as UTF-8, join, write TSV to stdout.

Re-export `load_tsv`, `dump_tsv`, `join_tables` from `tsvjoin`.
