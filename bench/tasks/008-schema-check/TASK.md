# Schema validator for nested dicts

Please build `schemacheck.validate(data, schema)` that checks a dict against a small schema language and returns `None` on success.

Schema format: a dict mapping field name → rule. A rule is a dict with:

- `"type"`: one of `"string"`, `"int"`, `"bool"`, `"dict"` (required)
- `"required"`: bool, default `True`
- `"fields"`: only for `"dict"` — a nested schema dict for that object

Rules:

- Extra keys in `data` not listed in the schema are ignored (allowed).
- Missing required fields → raise `schemacheck.ValidationError`.
- Wrong type → raise `ValidationError`.
- `bool` must be a real bool (not an int that happens to be 0/1).
- `int` must be a real int (not a bool).
- Optional fields (`required: False`) may be absent; if present they must type-check.
- Nested `"dict"` values must themselves be dicts and are validated with `"fields"` (which must be provided for dict types).

Export `validate` and `ValidationError` from `schemacheck`. `ValidationError` should subclass `ValueError`.
