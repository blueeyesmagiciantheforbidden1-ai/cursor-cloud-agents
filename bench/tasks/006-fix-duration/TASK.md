# Duration parser is wrong on several inputs

`durationparse.parse_duration(text)` should turn compact duration strings into a total number of seconds (int).

Supported units, each optional, in this order when present: `d` (days), `h` (hours), `m` (minutes), `s` (seconds). Examples:

- `"90s"` → `90`
- `"1h30m"` → `5400`
- `"1d"` → `86400`
- `"0s"` → `0`

Rules:

- Each unit may appear at most once.
- Numbers are non-negative integers (no decimals).
- Empty string, unknown characters, bare numbers without a unit, or out-of-order units (e.g. minutes before hours) should raise `ValueError`.
- Whitespace around the whole string should be ignored; internal spaces are invalid.

Please fix the implementation — the happy-path visible tests are not enough.
