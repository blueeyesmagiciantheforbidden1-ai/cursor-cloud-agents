# Dot paths

Convert between nested dicts and flat dotted-key dicts.

## API (`dotpaths`)

- `flatten(obj: dict) -> dict[str, object]`
- `expand(flat: dict[str, object]) -> dict`

## flatten

- Only plain `dict` values are nested further. Lists and other objects are leaves.
- Nested keys joined with `.` (no leading/trailing dots).
- Empty dict becomes `{}`.
- An empty nested dict should still produce a key whose value is `{}`
  (example: `{'a': {}}` -> `{'a': {}}`).
- Do not mutate the input.

## expand

- Build nested dicts from dotted keys.
- If two keys conflict (e.g. `a` is both a leaf and a parent), raise `ValueError`.
- Do not mutate the input.

Example: `flatten({'a': {'b': 1}, 'c': 2})` -> `{'a.b': 1, 'c': 2}`.
