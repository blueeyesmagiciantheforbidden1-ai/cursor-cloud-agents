# Split textkit and fix the helpers

`textkit` currently dumps three helpers into one `textkit/utils.py` file. Please
refactor so each helper lives in its own module, and fix the behaviour below.

## Layout

- `textkit/normalize.py` — `normalize_ws(text: str) -> str`
- `textkit/slugify.py` — `slugify(text: str) -> str`
- `textkit/truncate.py` — `truncate(text: str, max_len: int, *, word_boundary=False) -> str`
- `textkit/__init__.py` should re-export all three names.
- You may delete `utils.py` once the split is done.

## Behaviour

**normalize_ws**: strip leading/trailing whitespace and collapse any internal run of
whitespace (spaces, tabs, newlines) to a single space.

**slugify**: lowercase; replace any run of characters that are not ASCII letters or
digits with a single `-`; strip leading/trailing `-`. Example: `Hello, World!` ->
`hello-world`.

**truncate**: if `len(text) <= max_len`, return `text` unchanged. Otherwise return a
string of length at most `max_len` ending with `...` (the ellipsis counts toward
`max_len`). If `max_len < 3`, raise `ValueError`.
When `word_boundary=True`, do not break a word: cut at the last space before the
ellipsis would start, then add `...`. If there is no space to cut on, fall back to
a hard cut. Never return a string longer than `max_len`.

Visible tests are incomplete — they mostly check the happy path.
