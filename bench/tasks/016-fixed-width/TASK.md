# Fixed-width records

We get legacy flat files. Please implement parse/format helpers.

## Schema

A schema is a list of `(name: str, width: int)` pairs. Widths must be positive
integers. Field order in the schema is the field order on the line.

## API (`fixedwidth`)

- `parse_line(line: str, schema) -> dict[str, str]`
- `format_line(values: dict, schema) -> str`
- `parse_lines(text: str, schema) -> list[dict]` — split on newlines, skip empty
  lines, parse each remaining line.

## Parse

- If `line` still ends with `\n` or `\r`, strip one trailing newline sequence
  (`\r\n` or `\n` or `\r`) before measuring length.
- If the line length is less than the total schema width, raise `ValueError`.
- If the line is longer than the total width, ignore the excess on the right.
- Each field value is the corresponding slice **stripped** of leading/trailing spaces.

## Format

- For each field, take `str(values[name])` if present else `''`.
- If the string is longer than width, truncate on the right.
- If shorter, left-pad with spaces? No — **pad on the right with spaces**
  (left-aligned).
- Concatenate fields; do not add a trailing newline.
- Missing names in `values` are fine (empty field). Extra keys ignored.

Starter code is wrong on several of these points.
