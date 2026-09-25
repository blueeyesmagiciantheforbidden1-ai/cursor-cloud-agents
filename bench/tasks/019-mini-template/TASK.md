# Mini templates

A tiny `{{ ... }}` template system split across modules.

## Placeholders

Placeholders look like `{{name}}` or `{{name|filter}}` or `{{name|filter|filter}}`.
Whitespace inside braces is allowed (`{{ name | upper }}`). Names are identifiers
`[A-Za-z_][A-Za-z0-9_]*`. Unknown names raise `KeyError`. Unknown filters raise
`ValueError`.

## `minitemplate.filters`

Built-ins (operate on strings; if the value is not a str, convert with `str` first):

- `upper`, `lower`, `title`
- `strip`
- `len` — return `str(len(value))` (stringified length)

`get_filter(name) -> callable` looks up a filter; raise `ValueError` if missing.

## `minitemplate.engine`

- `render(template: str, context: dict) -> str`

## `minitemplate.loader`

- `TemplateLoader(directory)`
- `load(name: str) -> str` — read `directory/name` as UTF-8. Raise `FileNotFoundError`
  if missing. Reject names containing `..` or starting with `/` or `\\` via `ValueError`.
- `render(name: str, context: dict) -> str` — load then render.

Package re-exports `render`, `TemplateLoader`, `get_filter`.
