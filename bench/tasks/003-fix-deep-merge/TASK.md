# Deep merge is mutating our configs

`dicttools.deep_merge(base, override)` should return a new dict where:

- keys only in `base` are kept
- keys only in `override` are added
- when both values are dicts, they are merged recursively the same way
- otherwise the override value wins

We are seeing two problems in production:

1. Nested dicts are being replaced wholesale instead of merged.
2. The original `base` dict (and sometimes nested dicts inside it) is being changed by the call.

Please fix `deep_merge` so it returns a fresh nested structure and leaves both inputs unchanged. Non-dict values (lists, numbers, strings, etc.) are replaced, not concatenated.
