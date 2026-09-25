# Line filter

Small utility to keep or drop lines that contain a substring.

## Library (`linefilter`)

`filter_lines(lines, pattern, *, ignore_case=False, invert=False) -> list[str]`

- `lines` is an iterable of strings (do not mutate a passed-in list).
- Keep lines where `pattern` occurs as a substring.
- If `ignore_case` is true, compare case-insensitively.
- If `invert` is true, keep the lines that do **not** match.
- An empty `pattern` matches every line (so without invert you get all lines;
  with invert you get none).

## CLI

`python -m linefilter [--ignore-case] [--invert] PATTERN` reads stdin and writes
matching lines to stdout (preserving each line's original newline if present;
if a line had no trailing newline, still write it without adding one except for
normal print behavior — easiest: read with `sys.stdin` iteration and `print(line, end='')`
when the line already includes `\n`, otherwise `print(line)`.

Actually keep CLI simple: read all stdin lines via `sys.stdin.read().splitlines(keepends=True)`
and write kept lines with `sys.stdout.write`.

Args: optional `--ignore-case` / `-i`, optional `--invert` / `-v`, then exactly one PATTERN.

Visible tests cover the library function.
