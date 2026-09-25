# Tiny INI sections

Need a small INI helper (stdlib only — please do not use `configparser`).

## API (`inisections`)

- `parse(text: str) -> dict[str, dict[str, str]]`
- `dump(data: dict[str, dict[str, str]]) -> str`

## Parse rules

- Sections look like `[name]` on their own line (name stripped).
- Assignments are `key=value` (split on the **first** `=`). Strip the key; strip
  leading/trailing whitespace from the value.
- Blank lines ignored.
- Lines whose first non-whitespace character is `#` are comments (ignored).
- Duplicate keys in the same section: last one wins.
- A `key=value` line before any section header must raise `ValueError`.
- Invalid non-empty lines that are not a section and not an assignment raise `ValueError`.
- Section names and keys are case-sensitive.

## Dump rules

- Emit sections in the iteration order of the outer dict.
- Within a section, emit keys in that section dict's iteration order.
- Format: `[section]` then `key=value` lines. Put a blank line between sections
  (not after the last).
- No spaces around `=`.

Starter tests live in `tests/`.
